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
# WEBHOOK_URL должен быть базовым, например: https://myapp.onrender.com
WEBHOOK_URL_BASE = os.environ.get("WEBHOOK_URL") 
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
IMAGE_PROGRESS_KEY = "telegram_bot_image_progress" 

# --- Настройка логирования ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Уменьшаем "шум" от лишних библиотек
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Функции для работы с данными Redis (без изменений) ---
def load_list_data(key: str) -> list:
    try:
        data_json = redis_client.get(key)
        return json.loads(data_json) if data_json else []
    except Exception as e:
        logger.error(f"Ошибка при чтении списка из Redis по ключу '{key}': {e}")
        return []

def save_list_data(key: str, data: list):
    try:
        unique_data = list(dict.fromkeys(data))
        redis_client.set(key, json.dumps(unique_data))
    except Exception as e:
        logger.error(f"Ошибка при сохранении списка в Redis по ключу '{key}': {e}")

def load_dict_data(key: str) -> dict:
    try:
        data_json = redis_client.get(key)
        return json.loads(data_json) if data_json else {}
    except Exception as e:
        logger.error(f"Ошибка при чтении словаря из Redis по ключу '{key}': {e}")
        return {}

def save_dict_data(key: str, data: dict):
    try:
        redis_client.set(key, json.dumps(data))
    except Exception as e:
        logger.error(f"Ошибка при сохранении словаря в Redis по ключу '{key}': {e}")

# --- Логика постинга (без изменений) ---
async def handle_user_posting(context: ContextTypes.DEFAULT_TYPE, all_images: list):
    logger.info("Начало рассылки пользователям.")
    user_ids = load_list_data(USERS_KEY)
    if not user_ids:
        logger.info("Пользователи для рассылки не найдены.")
        return
    try:
        response = cloudinary.api.resources_by_asset_folder(
            CLOUDINARY_FOLDER, type="upload", max_results=500)
        current_images = response.get('resources', [])
    except Exception as e:
        logger.error(f"Не удалось получить список файлов для рассылки пользователям: {e}")
        return
    if not current_images:
        logger.warning("Нет изображений для рассылки пользователям.")
        return
    image_to_send = random.choice(current_images)
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
    if successful_sends > 0:
        try:
            cloudinary.uploader.destroy(image_public_id)
            logger.info(f"Изображение {image_public_id} разослано пользователям и удалено.")
        except Exception as e:
            logger.error(f"Ошибка при удалении файла {image_public_id} из Cloudinary: {e}")

async def handle_channel_posting(context: ContextTypes.DEFAULT_TYPE, all_images: list):
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
    images_pool = all_images.copy()
    random.shuffle(images_pool)
    for channel_id in channels:
        image_sent_to_channel = False
        for i in range(len(images_pool) - 1, -1, -1):
            image = images_pool[i]
            public_id = image['public_id']
            if channel_id not in progress.get(public_id, []):
                try:
                    await context.bot.send_photo(chat_id=channel_id, photo=image['secure_url'])
                    logger.info(f"Отправлено изображение {public_id} в канал {channel_id}.")
                    progress.setdefault(public_id, []).append(channel_id)
                    del images_pool[i]
                    image_sent_to_channel = True
                    await asyncio.sleep(0.1)
                    break 
                except Exception as e:
                    logger.error(f"Не удалось отправить {public_id} в {channel_id}: {e}")
        if not image_sent_to_channel:
            logger.warning(f"Для канала {channel_id} не нашлось ни одного нового изображения.")
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"Внимание! Для канала {channel_id} закончились уникальные изображения.")
    completed_ids = [pid for pid, sent_to in progress.items() if all_channels_set.issubset(set(sent_to))]
    if completed_ids:
        logger.info(f"Найдены завершенные изображения для удаления: {completed_ids}")
        try:
            for i in range(0, len(completed_ids), 100):
                chunk = completed_ids[i:i + 100]
                cloudinary.api.delete_resources(chunk)
                logger.info(f"Успешно удалена пачка из {len(chunk)} изображений.")
        except Exception as e:
            logger.error(f"Ошибка при массовом удалении из Cloudinary: {e}")
        for public_id in completed_ids:
            if public_id in progress: del progress[public_id]
    save_dict_data(IMAGE_PROGRESS_KEY, progress)

async def post_image_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Запуск основной задачи по отправке изображений.")
    try:
        response = cloudinary.api.resources_by_asset_folder(CLOUDINARY_FOLDER, type="upload", max_results=250)
        all_images = response.get('resources', [])
    except Exception as e:
        logger.error(f"Критическая ошибка: не удалось получить список файлов из Cloudinary: {e}")
        await context.bot.send_message(chat_id=ADMIN_USER_ID, text=f"Ошибка при получении списка файлов из Cloudinary: {e}")
        return
    await handle_channel_posting(context, all_images)
    await handle_user_posting(context, all_images)
    logger.info("Основная задача по отправке изображений завершена.")

# --- Обработчики команд (без изменений) ---
async def save_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Получаю картинку и загружаю в облако...")
    photo = update.message.photo[-1]
    try:
        new_file = await photo.get_file()
        upload_result = cloudinary.uploader.upload(new_file.file_path, folder=CLOUDINARY_FOLDER)
        file_url = upload_result.get('secure_url')
        logger.info(f"Администратор добавил новую картинку в Cloudinary: {file_url}")
        await update.message.reply_text("Картинка успешно сохранена в облачное хранилище!")
    except Exception as e:
        logger.error(f"Не удалось сохранить картинку в Cloudinary: {e}")
        await update.message.reply_text(f"Произошла ошибка при сохранении в облако: {e}")

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
    if update.effective_user.id != ADMIN_USER_ID: return
    try:
        channel_id = context.args[0]
        channels = load_list_data(CHANNELS_KEY)
        if channel_id not in channels:
            channels.append(channel_id)
            save_list_data(CHANNELS_KEY, channels)
            await update.message.reply_text(f"Канал {channel_id} добавлен.")
        else:
            await update.message.reply_text(f"Канал {channel_id} уже в списке.")
    except IndexError:
        await update.message.reply_text("Использование: /addchannel <code>@имя_канала</code>", parse_mode='HTML')

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: return
    try:
        channel_id = context.args[0]
        channels = load_list_data(CHANNELS_KEY)
        if channel_id in channels:
            channels.remove(channel_id)
            save_list_data(CHANNELS_KEY, channels)
            await update.message.reply_text(f"Канал {channel_id} удален.")
        else:
            await update.message.reply_text(f"Канала {channel_id} нет в списке.")
    except IndexError:
        await update.message.reply_text("Использование: /removechannel <code>@имя_канала</code>", parse_mode='HTML')

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: return
    channels = load_list_data(CHANNELS_KEY)
    message = "<b>Каналы для постинга:</b>\n" + "\n".join(f"<code>{c}</code>" for c in channels) if channels else "Список каналов пуст."
    await update.message.reply_text(message, parse_mode='HTML')
    
async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: return
    user_ids = load_list_data(USERS_KEY)
    message = f"<b>Подписчики (всего {len(user_ids)}):</b>\n\n" + "\n".join([str(uid) for uid in user_ids]) if user_ids else "Пока нет подписчиков."
    await update.message.reply_text(message, parse_mode='HTML')

async def force_post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: return
    await update.message.reply_text("Принудительно запускаю рассылку...")
    context.application.create_task(post_image_job(context), update=update)

async def next_post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: return
    jobs = context.application.job_queue.get_jobs_by_name("post_image_job")
    if not jobs:
        await update.message.reply_text("Задача рассылки не найдена.")
        return
    next_run_time = jobs[0].next_t
    if not next_run_time:
        await update.message.reply_text("Время следующего запуска не определено.")
        return
    now = datetime.now(timezone.utc)
    time_remaining = next_run_time - now
    if time_remaining.total_seconds() > 0:
        hours, rem = divmod(int(time_remaining.total_seconds()), 3600)
        minutes, seconds = divmod(rem, 60)
        message = f"Следующая отправка изображений через: {hours} ч, {minutes} мин, {seconds} сек."
    else:
        message = "Рассылка должна была уже начаться или начнется с минуты на минуту."
    await update.message.reply_text(message)
    
# <<< НОВАЯ ФУНКЦИЯ ДЛЯ UPTIMEROBOT >>>
async def health_check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Пустой обработчик, чтобы отвечать на пинги от UptimeRobot.
    Он не будет ничего делать с обновлением, но сам факт его получения
    и успешного завершения не даст Render уснуть.
    """
    logger.info("Пинг от сервиса мониторинга успешно обработан.")
    # Специально ничего не делаем и не отвечаем пользователю.


def main() -> None:
    """Запускает бота."""
    # Убеждаемся, что URL для вебхука существует
    if not WEBHOOK_URL_BASE:
        logger.error("Переменная окружения WEBHOOK_URL не установлена!")
        return

    # Создаем приложение
    ptb_app = Application.builder().token(BOT_TOKEN).build()
    
    # Добавляем обработчики команд
    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(CommandHandler("stop", stop))
    ptb_app.add_handler(CommandHandler("addchannel", add_channel))
    ptb_app.add_handler(CommandHandler("removechannel", remove_channel))
    ptb_app.add_handler(CommandHandler("listchannels", list_channels))
    ptb_app.add_handler(CommandHandler("listusers", list_users))
    ptb_app.add_handler(CommandHandler("forcepost", force_post_command))
    ptb_app.add_handler(CommandHandler("nextpost", next_post_command))
    
    # Обработчик для фото от админа
    ptb_app.add_handler(MessageHandler(filters.PHOTO & filters.User(user_id=ADMIN_USER_ID) & ~filters.COMMAND, save_photo_handler))

    # <<< ДОБАВЛЯЕМ ОБРАБОТЧИК ДЛЯ UPTIMEROBOT >>>
    # Он будет срабатывать на любой входящий запрос, который не является одной из команд выше.
    # Так как UptimeRobot шлет пустой запрос, он попадет сюда.
    # Ставим ему низкий priority, чтобы он не перехватывал команды.
    ptb_app.add_handler(MessageHandler(filters.ALL, health_check_handler), group=1)

    # Настраиваем повторяющуюся задачу
    if ptb_app.job_queue:
        ptb_app.job_queue.run_repeating(post_image_job, interval=10800, name="post_image_job")

    # Формируем полный URL для вебхука
    webhook_full_url = f"{WEBHOOK_URL_BASE.rstrip('/')}/{BOT_TOKEN}"

    # Запускаем вебхук
    ptb_app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=webhook_full_url,
        # Добавляем параметр, чтобы проверять, совпадает ли текущий вебхук
        # с тем, что мы хотим установить. Это уменьшит кол-во лишних запросов.
        url_path=BOT_TOKEN,
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == "__main__":
    main()

