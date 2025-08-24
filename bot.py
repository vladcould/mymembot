import os
import random
import json
import logging
import asyncio
import cloudinary
import cloudinary.api
import cloudinary.uploader
import redis
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8443))

# --- Конфигурация Cloudinary ---
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True
)
CLOUDINARY_FOLDER = "telegram_bot_images"

# --- Подключение к Redis ---
REDIS_URL = os.environ.get("REDIS_URL")
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
CHANNELS_KEY = "telegram_bot_channels"
USERS_KEY = "telegram_bot_users"
# <<< НОВЫЙ КЛЮЧ для отслеживания отправленных изображений
SENT_IMAGES_KEY = "telegram_bot_sent_images" 

# --- Настройка логирования ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Функции для работы с данными Redis (без изменений) ---
def load_data(key: str) -> list:
    try:
        data_json = redis_client.get(key)
        if data_json:
            return json.loads(data_json)
        return []
    except Exception as e:
        logger.error(f"Ошибка при чтении данных из Redis по ключу '{key}': {e}")
        return []

def save_data(key: str, data: list):
    try:
        # Для SENT_IMAGES_KEY нам не нужна уникальность, т.к. мы управляем этим сами
        if key in [CHANNELS_KEY, USERS_KEY]:
             data = list(dict.fromkeys(data))
        redis_client.set(key, json.dumps(data))
    except Exception as e:
        logger.error(f"Ошибка при сохранении данных в Redis по ключу '{key}': {e}")


# --- ОСНОВНАЯ ЛОГИКА БОТА ДЛЯ ПОСТИНГА (ПОЛНОСТЬЮ ПЕРЕРАБОТАНА) ---
async def post_image_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Запуск задачи по отправке изображений.")
    
    # 1. Получаем список всех изображений из Cloudinary
    try:
        response = cloudinary.api.resources_by_asset_folder(
            CLOUDINARY_FOLDER, type="upload", max_results=500
        )
        all_images = response.get('resources', [])
    except Exception as e:
        logger.error(f"Не удалось получить список файлов из Cloudinary: {e}")
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"Произошла ошибка при получении списка файлов из Cloudinary: {e}"
        )
        return

    if not all_images:
        logger.warning("В Cloudinary нет изображений для отправки.")
        await context.bot.send_message(chat_id=ADMIN_USER_ID, text="Внимание! В Cloudinary нет изображений.")
        return

    # 2. Получаем ID уже отправленных изображений из Redis
    sent_image_ids = set(load_data(SENT_IMAGES_KEY))
    
    # 3. Определяем, какие изображения еще не были отправлены
    available_images = [img for img in all_images if img['public_id'] not in sent_image_ids]

    # 4. Проверяем, если все изображения уже разосланы (цикл завершен)
    if not available_images and sent_image_ids:
        logger.info("Все изображения были отправлены. Начинаю новый цикл и очистку.")
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"Все {len(sent_image_ids)} изображений были разосланы. Начинаю очистку и новый цикл."
        )
        
        # Удаляем старые изображения из Cloudinary
        ids_to_delete = list(sent_image_ids)
        # Cloudinary API позволяет удалять до 100 ID за раз
        for i in range(0, len(ids_to_delete), 100):
            chunk = ids_to_delete[i:i + 100]
            try:
                cloudinary.api.delete_resources(chunk)
                logger.info(f"Успешно удалена пачка из {len(chunk)} изображений из Cloudinary.")
            except Exception as e:
                logger.error(f"Ошибка при массовом удалении изображений из Cloudinary: {e}")

        # Сбрасываем список отправленных и начинаем заново
        save_data(SENT_IMAGES_KEY, [])
        sent_image_ids = set()
        available_images = all_images
        logger.info("Очистка завершена. Доступны все изображения для нового цикла.")

    # 5. Собираем всех получателей
    channels = load_data(CHANNELS_KEY)
    user_ids = load_data(USERS_KEY)
    all_targets = channels + user_ids
    
    if not all_targets:
        logger.info("Нет получателей для рассылки (каналов или пользователей).")
        return

    random.shuffle(available_images)
    
    newly_sent_ids = []
    
    # 6. Отправляем каждому получателю уникальное изображение
    for target_id in all_targets:
        if not available_images:
            logger.warning("Доступные изображения закончились в середине рассылки.")
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text="Внимание! Изображения закончились раньше, чем все получатели их получили. Загрузите больше картинок."
            )
            break # Прерываем цикл, если картинки кончились

        image_to_send = available_images.pop()
        image_url = image_to_send['secure_url']
        image_public_id = image_to_send['public_id']
        
        try:
            await context.bot.send_photo(chat_id=target_id, photo=image_url)
            logger.info(f"Изображение {image_public_id} успешно отправлено в {target_id}.")
            newly_sent_ids.append(image_public_id)
            await asyncio.sleep(0.1) # Небольшая задержка
        except Exception as e:
            logger.error(f"Не удалось отправить изображение {image_public_id} в {target_id}: {e}")
            # Если не удалось, возвращаем картинку в пул доступных
            available_images.append(image_to_send)
    
    # 7. Обновляем список отправленных изображений в Redis
    if newly_sent_ids:
        final_sent_list = list(sent_image_ids) + newly_sent_ids
        save_data(SENT_IMAGES_KEY, final_sent_list)
        logger.info(f"Сохранен обновленный список из {len(final_sent_list)} отправленных изображений.")


# --- Обработчик сохранения фото (без изменений) ---
async def save_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Получаю картинку и загружаю в облако...")
    photo = update.message.photo[-1]
    
    try:
        new_file = await photo.get_file()
        upload_result = cloudinary.uploader.upload(
            new_file.file_path,
            folder=CLOUDINARY_FOLDER
        )
        file_url = upload_result.get('secure_url')
        logger.info(f"Администратор добавил новую картинку в Cloudinary: {file_url}")
        await update.message.reply_text("Картинка успешно сохранена в облачное хранилище!")
    except Exception as e:
        logger.error(f"Не удалось сохранить картинку в Cloudinary: {e}")
        await update.message.reply_text(f"Произошла ошибка при сохранении в облако: {e}")

# --- Команды бота (без существенных изменений, только мелкие правки) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    all_users = load_data(USERS_KEY)
    if user.id == ADMIN_USER_ID:
        if user.id not in all_users:
            all_users.append(user.id)
            save_data(USERS_KEY, all_users)
        admin_text = ("<b>Админ-панель:</b>\n\n"
                      "/addchannel <code>@имя_канала</code> - Добавить канал\n"
                      "/removechannel <code>@имя_канала</code> - Удалить канал\n"
                      "/listchannels - Показать список каналов\n"
                      "/listusers - Показать всех подписчиков\n"
                      "/forcepost - Запустить рассылку немедленно\n"
                      "/nextpost - Время до следующей рассылки")
        await update.message.reply_text(admin_text, parse_mode='HTML')
        return
    if user.id not in all_users:
        all_users.append(user.id)
        save_data(USERS_KEY, all_users)
        logger.info(f"Новый подписчик: {user.first_name} (ID: {user.id})")
        await update.message.reply_text("Привет! Вы подписались на рассылку картинок.\nЧтобы отписаться, используйте /stop.")
    else:
        await update.message.reply_text("Вы уже подписаны на рассылку.")

async def next_post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await unauthorized_user_reply(update, context)
        return
    job = context.bot_data.get('post_job')
    if not job:
        await update.message.reply_text("Задача рассылки не найдена или еще не была запущена.")
        return
    next_run_time = job.next_t
    if not next_run_time:
        await update.message.reply_text("Не удалось определить время следующего запуска.")
        return
    now = datetime.now(timezone.utc)
    time_remaining = next_run_time - now
    if time_remaining.total_seconds() > 0:
        hours, remainder = divmod(int(time_remaining.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        message = f"Следующая отправка изображений через: {hours} ч, {minutes} мин, {seconds} сек."
    else:
        message = "Рассылка должна была уже начаться или начнется с минуты на минуту."
    await update.message.reply_text(message)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_ids = load_data(USERS_KEY)
    if user_id in user_ids:
        user_ids.remove(user_id)
        save_data(USERS_KEY, user_ids)
        await update.message.reply_text("Вы успешно отписались.")
    else:
        await update.message.reply_text("Вы и не были подписаны.")

async def unauthorized_user_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Извините, эта команда только для администратора.")

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    try:
        channel_id = context.args[0]
        channels = load_data(CHANNELS_KEY)
        if channel_id not in channels:
            channels.append(channel_id)
            save_data(CHANNELS_KEY, channels)
            await update.message.reply_text(f"Канал {channel_id} добавлен.")
        else:
            await update.message.reply_text(f"Канал {channel_id} уже в списке.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /addchannel <code>@имя_канала</code>", parse_mode='HTML')

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    try:
        channel_id = context.args[0]
        channels = load_data(CHANNELS_KEY)
        if channel_id in channels:
            channels.remove(channel_id)
            save_data(CHANNELS_KEY, channels)
            await update.message.reply_text(f"Канал {channel_id} удален.")
        else:
            await update.message.reply_text(f"Канала {channel_id} нет в списке.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /removechannel <code>@имя_канала</code>", parse_mode='HTML')

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    channels = load_data(CHANNELS_KEY)
    message = "<b>Каналы для постинга:</b>\n" + "\n".join(f"<code>{c}</code>" for c in channels) if channels else "Список каналов пуст."
    await update.message.reply_text(message, parse_mode='HTML')

async def force_post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    await update.message.reply_text("Принудительно запускаю рассылку...")
    context.application.create_task(post_image_job(context), update=update)

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    user_ids = load_data(USERS_KEY)
    if not user_ids: await update.message.reply_text("Пока нет подписчиков."); return
    message = f"<b>Подписчики (всего {len(user_ids)}):</b>\n\n" + "\n".join([str(uid) for uid in user_ids])
    await update.message.reply_text(message, parse_mode='HTML')


async def post_init(application: Application):
    await application.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}", allowed_updates=Update.ALL_TYPES)
    logger.info(f"Вебхук установлен на {WEBHOOK_URL}/{BOT_TOKEN}")

def main() -> None:
    ptb_app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(CommandHandler("stop", stop))
    ptb_app.add_handler(CommandHandler("addchannel", add_channel))
    ptb_app.add_handler(CommandHandler("removechannel", remove_channel))
    ptb_app.add_handler(CommandHandler("listchannels", list_channels))
    ptb_app.add_handler(CommandHandler("listusers", list_users))
    ptb_app.add_handler(CommandHandler("forcepost", force_post_command))
    ptb_app.add_handler(CommandHandler("nextpost", next_post_command))
    ptb_app.add_handler(MessageHandler(filters.PHOTO & filters.User(user_id=ADMIN_USER_ID) & ~filters.COMMAND, save_photo_handler))

    job_queue = ptb_app.job_queue
    if job_queue:
        post_job = job_queue.run_repeating(post_image_job, interval=10800) # 3 часа
        ptb_app.bot_data['post_job'] = post_job

    ptb_app.run_webhook(listen="0.0.0.0", port=PORT, webhook_url=WEBHOOK_URL)


if __name__ == "__main__":
    main()
