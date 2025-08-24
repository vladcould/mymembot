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
# <<< НОВЫЙ КЛЮЧ для отслеживания прогресса по каналам
IMAGE_PROGRESS_KEY = "telegram_bot_image_progress" 

# --- Настройка логирования ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Функции для работы с данными Redis ---
def load_list_data(key: str) -> list:
    """Загружает список из Redis."""
    try:
        data_json = redis_client.get(key)
        return json.loads(data_json) if data_json else []
    except Exception as e:
        logger.error(f"Ошибка при чтении списка из Redis по ключу '{key}': {e}")
        return []

def save_list_data(key: str, data: list):
    """Сохраняет список в Redis, обеспечивая уникальность элементов."""
    try:
        unique_data = list(dict.fromkeys(data))
        redis_client.set(key, json.dumps(unique_data))
    except Exception as e:
        logger.error(f"Ошибка при сохранении списка в Redis по ключу '{key}': {e}")

def load_dict_data(key: str) -> dict:
    """Загружает словарь (JSON-объект) из Redis."""
    try:
        data_json = redis_client.get(key)
        return json.loads(data_json) if data_json else {}
    except Exception as e:
        logger.error(f"Ошибка при чтении словаря из Redis по ключу '{key}': {e}")
        return {}

def save_dict_data(key: str, data: dict):
    """Сохраняет словарь в Redis."""
    try:
        redis_client.set(key, json.dumps(data))
    except Exception as e:
        logger.error(f"Ошибка при сохранении словаря в Redis по ключу '{key}': {e}")


# --- РАЗДЕЛЕНИЕ ЛОГИКИ ПОСТИНГА ---

async def handle_user_posting(context: ContextTypes.DEFAULT_TYPE, all_images: list):
    """Обрабатывает рассылку пользователям (одно изображение для всех)."""
    logger.info("Начало рассылки пользователям.")
    user_ids = load_list_data(USERS_KEY)
    if not user_ids:
        logger.info("Пользователи для рассылки не найдены.")
        return

    if not all_images:
        logger.warning("Нет изображений для рассылки пользователям.")
        return

    image_to_send = random.choice(all_images)
    image_url = image_to_send['secure_url']
    image_public_id = image_to_send['public_id']
    logger.info(f"Для пользователей выбрано изображение: {image_public_id}")

    successful_sends = 0
    for user_id in user_ids:
        try:
            await context.bot.send_photo(chat_id=user_id, photo=image_url)
            successful_sends += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.warning(f"Не удалось отправить пользователю {user_id}: {e}")

    # Если хотя бы одна отправка была успешной, удаляем файл
    if successful_sends > 0:
        try:
            # Важно: это изображение может быть еще нужно для каналов.
            # По ТЗ для юзеров логика своя, поэтому оно удаляется.
            # Если нужно, чтобы оно не удалялось, эту строку надо убрать.
            cloudinary.uploader.destroy(image_public_id)
            logger.info(f"Изображение {image_public_id} разослано пользователям и удалено.")
        except Exception as e:
            logger.error(f"Ошибка при удалении файла {image_public_id} из Cloudinary: {e}")

async def handle_channel_posting(context: ContextTypes.DEFAULT_TYPE, all_images: list):
    """Обрабатывает рассылку по каналам (уникальное изображение для каждого)."""
    logger.info("Начало рассылки по каналам.")
    channels = load_list_data(CHANNELS_KEY)
    if not channels:
        logger.info("Каналы для рассылки не найдены.")
        return

    if not all_images:
        logger.warning("Нет изображений для рассылки по каналам.")
        await context.bot.send_message(chat_id=ADMIN_USER_ID, text="В Cloudinary нет изображений для рассылки по каналам.")
        return

    progress = load_dict_data(IMAGE_PROGRESS_KEY)
    all_channels_set = set(channels)
    
    # Перемешиваем изображения, чтобы рассылка была более случайной
    random.shuffle(all_images)

    # 1. Отправляем по одному изображению в каждый канал
    for channel_id in channels:
        sent_this_run = False
        for image in all_images:
            public_id = image['public_id']
            # Проверяем, было ли это изображение уже отправлено в ЭТОТ канал
            if channel_id not in progress.get(public_id, []):
                try:
                    await context.bot.send_photo(chat_id=channel_id, photo=image['secure_url'])
                    logger.info(f"Отправлено изображение {public_id} в канал {channel_id}.")
                    # Обновляем прогресс
                    progress.setdefault(public_id, []).append(channel_id)
                    sent_this_run = True
                    await asyncio.sleep(0.1)
                    break # Переходим к следующему каналу
                except Exception as e:
                    logger.error(f"Не удалось отправить {public_id} в {channel_id}: {e}")
        
        if not sent_this_run:
            logger.warning(f"Для канала {channel_id} не нашлось ни одного нового изображения.")
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"Внимание! Для канала {channel_id} закончились уникальные изображения. Цикл скоро начнется заново.")

    # 2. Проверяем, какие изображения завершили свой цикл (отправлены во все каналы)
    completed_ids = []
    for public_id, sent_to_channels in progress.items():
        if all_channels_set.issubset(set(sent_to_channels)):
            completed_ids.append(public_id)
    
    # 3. Удаляем завершенные изображения
    if completed_ids:
        logger.info(f"Найдены завершенные изображения для удаления: {completed_ids}")
        try:
            # Cloudinary API позволяет удалять до 100 ID за раз
            for i in range(0, len(completed_ids), 100):
                chunk = completed_ids[i:i + 100]
                cloudinary.api.delete_resources(chunk)
                logger.info(f"Успешно удалена пачка из {len(chunk)} изображений.")
        except Exception as e:
            logger.error(f"Ошибка при массовом удалении из Cloudinary: {e}")
        
        # Удаляем записи о них из нашего трекера прогресса
        for public_id in completed_ids:
            del progress[public_id]
            
    # 4. Сохраняем обновленный прогресс в Redis
    save_dict_data(IMAGE_PROGRESS_KEY, progress)


async def post_image_job(context: ContextTypes.DEFAULT_TYPE):
    """Основная задача, которая запускает постинг для пользователей и каналов."""
    logger.info("Запуск основной задачи по отправке изображений.")
    
    try:
        response = cloudinary.api.resources_by_asset_folder(
            CLOUDINARY_FOLDER, type="upload", max_results=500
        )
        all_images = response.get('resources', [])
    except Exception as e:
        logger.error(f"Критическая ошибка: не удалось получить список файлов из Cloudinary: {e}")
        await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"Ошибка при получении списка файлов из Cloudinary: {e}")
        return

    # Запускаем обе логики
    await handle_channel_posting(context, all_images)
    await handle_user_posting(context, all_images)
    logger.info("Основная задача по отправке изображений завершена.")


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


# --- Команды бота (используем новые функции для работы с Redis) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    all_users = load_list_data(USERS_KEY)
    if user.id == ADMIN_USER_ID:
        if user.id not in all_users:
            all_users.append(user.id)
            save_list_data(USERS_KEY, all_users)
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
        save_list_data(USERS_KEY, all_users)
        logger.info(f"Новый подписчик: {user.first_name} (ID: {user.id})")
        await update.message.reply_text("Привет! Вы подписались на рассылку картинок.\nЧтобы отписаться, используйте /stop.")
    else:
        await update.message.reply_text("Вы уже подписаны на рассылку.")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_ids = load_list_data(USERS_KEY)
    if user_id in user_ids:
        user_ids.remove(user_id)
        save_list_data(USERS_KEY, user_ids)
        await update.message.reply_text("Вы успешно отписались.")
    else:
        await update.message.reply_text("Вы и не были подписаны.")

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    try:
        channel_id = context.args[0]
        channels = load_list_data(CHANNELS_KEY)
        if channel_id not in channels:
            channels.append(channel_id)
            save_list_data(CHANNELS_KEY, channels)
            await update.message.reply_text(f"Канал {channel_id} добавлен.")
        else:
            await update.message.reply_text(f"Канал {channel_id} уже в списке.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /addchannel <code>@имя_канала</code>", parse_mode='HTML')

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    try:
        channel_id = context.args[0]
        channels = load_list_data(CHANNELS_KEY)
        if channel_id in channels:
            channels.remove(channel_id)
            save_list_data(CHANNELS_KEY, channels)
            await update.message.reply_text(f"Канал {channel_id} удален.")
        else:
            await update.message.reply_text(f"Канала {channel_id} нет в списке.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /removechannel <code>@имя_канала</code>", parse_mode='HTML')

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    channels = load_list_data(CHANNELS_KEY)
    message = "<b>Каналы для постинга:</b>\n" + "\n".join(f"<code>{c}</code>" for c in channels) if channels else "Список каналов пуст."
    await update.message.reply_text(message, parse_mode='HTML')
    
async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    user_ids = load_list_data(USERS_KEY)
    if not user_ids: await update.message.reply_text("Пока нет подписчиков."); return
    message = f"<b>Подписчики (всего {len(user_ids)}):</b>\n\n" + "\n".join([str(uid) for uid in user_ids])
    await update.message.reply_text(message, parse_mode='HTML')

async def force_post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    await update.message.reply_text("Принудительно запускаю рассылку...")
    context.application.create_task(post_image_job(context), update=update)

# --- Остальные команды без изменений ---

async def unauthorized_user_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Извините, эта команда только для администратора.")

async def next_post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await unauthorized_user_reply(update, context)
        return
    job = context.bot_data.get('post_job')
    if not job or not job.next_t:
        await update.message.reply_text("Задача рассылки не найдена или время следующего запуска не определено.")
        return
    now = datetime.now(timezone.utc)
    time_remaining = job.next_t - now
    if time_remaining.total_seconds() > 0:
        hours, rem = divmod(int(time_remaining.total_seconds()), 3600)
        minutes, seconds = divmod(rem, 60)
        message = f"Следующая отправка изображений через: {hours} ч, {minutes} мин, {seconds} сек."
    else:
        message = "Рассылка должна была уже начаться или начнется с минуты на минуту."
    await update.message.reply_text(message)

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
        post_job = job_queue.run_repeating(post_image_job, interval=10800)
        ptb_app.bot_data['post_job'] = post_job

    ptb_app.run_webhook(listen="0.0.0.0", port=PORT, webhook_url=WEBHOOK_URL)

if __name__ == "__main__":
    main()
