import os
import random
import json
import logging
import asyncio
import cloudinary
import cloudinary.api
import cloudinary.uploader
import redis # <-- НОВЫЙ ИМПОРТ
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

# --- НОВЫЙ БЛОК: Подключение к Redis ---
# Получаем URL для подключения из переменных окружения, которые мы добавили на Render
REDIS_URL = os.environ.get("REDIS_URL")
# Создаем клиент для работы с базой данных Redis. decode_responses=True означает,
# что данные из Redis будут автоматически преобразовываться в строки.
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
CHANNELS_KEY = "telegram_bot_channels" # Название "ключа" для хранения каналов в Redis
USERS_KEY = "telegram_bot_users"       # Название "ключа" для хранения пользователей в Redis

# --- Настройка логирования ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- ИЗМЕНЕНО: Функции для работы с данными теперь используют Redis ---
def load_data(key: str) -> list:
    """Загружает данные из Redis по ключу."""
    try:
        data_json = redis_client.get(key)
        if data_json:
            return json.loads(data_json) # Преобразуем JSON-строку обратно в список Python
        return []
    except Exception as e:
        logger.error(f"Ошибка при чтении данных из Redis по ключу '{key}': {e}")
        return []

def save_data(key: str, data: list):
    """Сохраняет данные в Redis по ключу."""
    try:
        # Убираем дубликаты перед сохранением
        unique_data = list(dict.fromkeys(data))
        # Преобразуем список Python в JSON-строку для хранения в Redis
        redis_client.set(key, json.dumps(unique_data))
    except Exception as e:
        logger.error(f"Ошибка при сохранении данных в Redis по ключу '{key}': {e}")


# --- Основная логика бота для постинга (исправлена опечатка photo_url) ---
async def post_image_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Запуск задачи по отправке изображений из Cloudinary.")

    try:
        response = cloudinary.api.resources_by_asset_folder(
            CLOUDINARY_FOLDER, type="upload", max_results=500
        )
        images = response.get('resources', [])
    except Exception as e:
        logger.error(f"Не удалось получить список файлов из Cloudinary: {e}")
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"Произошла ошибка при получении списка файлов из Cloudinary."
        )
        return

    if not images:
        logger.warning("Изображения в Cloudinary закончились.")
        await context.bot.send_message(chat_id=ADMIN_USER_ID, text="Внимание! Все изображения в Cloudinary закончились.")
        return

    image_to_send = random.choice(images)
    image_url = image_to_send['secure_url']
    image_public_id = image_to_send['public_id']
    logger.info(f"Выбрано изображение для рассылки: {image_url} (Public ID: {image_public_id})")

    # ИЗМЕНЕНО: Загружаем данные из Redis
    channels = load_data(CHANNELS_KEY)
    successful_sends = 0

    if channels:
        for channel_id in channels:
            try:
                await context.bot.send_photo(chat_id=channel_id, photo=image_url)
                successful_sends += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"Не удалось отправить в канал {channel_id}: {e}")

    # ИЗМЕНЕНО: Загружаем данные из Redis
    user_ids = load_data(USERS_KEY)
    if user_ids:
        for user_id in user_ids:
            try:
                # ИСПРАВЛЕНО: была опечатка photo_url, заменено на image_url
                await context.bot.send_photo(chat_id=user_id, photo=image_url)
                successful_sends += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.warning(f"Не удалось отправить пользователю {user_id}: {e}")

    if successful_sends > 0:
        try:
            cloudinary.uploader.destroy(image_public_id)
            logger.info(f"Изображение {image_public_id} успешно разослано и удалено из Cloudinary.")
        except Exception as e:
            logger.error(f"Ошибка при удалении файла {image_public_id} из Cloudinary: {e}")

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

# --- Команды бота, переведенные на Redis ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # ИЗМЕНЕНО: работаем с Redis
    all_users = load_data(USERS_KEY)
    if user.id == ADMIN_USER_ID:
        if user.id not in all_users:
            all_users.append(user.id)
            save_data(USERS_KEY, all_users) # ИЗМЕНЕНО
        admin_text = ("<b>Админ-панель:</b>\n\n"
                      "/addchannel <code>@имя_канала</code> - Добавить канал\n"
                      "/removechannel <code>@имя_канала</code> - Удалить канал\n"
                      "/listchannels - Показать список каналов\n"
                      "/listusers - Показать всех подписчиков\n"
                      "/forcepost - Запустить рассылку немедленно")
        await update.message.reply_text(admin_text, parse_mode='HTML')
        return
    if user.id not in all_users:
        all_users.append(user.id)
        save_data(USERS_KEY, all_users) # ИЗМЕНЕНО
        logger.info(f"Новый подписчик: {user.first_name} (ID: {user.id})")
        await update.message.reply_text("Привет! Вы подписались на рассылку картинок.\nЧтобы отписаться, используйте /stop.")
    else:
        await update.message.reply_text("Вы уже подписаны на рассылку.")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_ids = load_data(USERS_KEY) # ИЗМЕНЕНО
    if user_id in user_ids:
        user_ids.remove(user_id)
        save_data(USERS_KEY, user_ids) # ИЗМЕНЕНО
        await update.message.reply_text("Вы успешно отписались.")
    else:
        await update.message.reply_text("Вы и не были подписаны.")

async def unauthorized_user_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Извините, эта команда только для администратора.")

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    try:
        channel_id = context.args[0]
        channels = load_data(CHANNELS_KEY) # ИЗМЕНЕНО
        if channel_id not in channels:
            channels.append(channel_id)
            save_data(CHANNELS_KEY, channels) # ИЗМЕНЕНО
            await update.message.reply_text(f"Канал {channel_id} добавлен.")
        else:
            await update.message.reply_text(f"Канал {channel_id} уже в списке.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /addchannel <code>@имя_канала</code>", parse_mode='HTML')

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    try:
        channel_id = context.args[0]
        channels = load_data(CHANNELS_KEY) # ИЗМЕНЕНО
        if channel_id in channels:
            channels.remove(channel_id)
            save_data(CHANNELS_KEY, channels) # ИЗМЕНЕНО
            await update.message.reply_text(f"Канал {channel_id} удален.")
        else:
            await update.message.reply_text(f"Канала {channel_id} нет в списке.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /removechannel <code>@имя_канала</code>", parse_mode='HTML')

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    channels = load_data(CHANNELS_KEY) # ИЗМЕНЕНО
    message = "<b>Каналы для постинга:</b>\n" + "\n".join(f"<code>{c}</code>" for c in channels) if channels else "Список каналов пуст."
    await update.message.reply_text(message, parse_mode='HTML')

async def force_post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    await update.message.reply_text("Принудительно запускаю рассылку из Cloudinary...")
    context.application.create_task(post_image_job(context), update=update)

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    user_ids = load_data(USERS_KEY) # ИЗМЕНЕНО
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
    ptb_app.add_handler(MessageHandler(filters.PHOTO & filters.User(user_id=ADMIN_USER_ID) & ~filters.COMMAND, save_photo_handler))
    
    job_queue = ptb_app.job_queue
    if job_queue:
        job_queue.run_repeating(post_image_job, interval=10800, first=10)

    ptb_app.run_webhook(listen="0.0.0.0", port=PORT, webhook_url=WEBHOOK_URL)

if __name__ == "__main__":
    main()
