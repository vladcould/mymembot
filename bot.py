import os
import random
import json
import logging
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- КОНФИГУРАЦИЯ ---
# Обязательно убедитесь, что все эти переменные заданы в настройках окружения на Render
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") # Например, https://your-app-name.onrender.com
# Render автоматически предоставляет переменную PORT
PORT = int(os.environ.get("PORT", 8443))

# --- Названия файлов для хранения данных ---
# ВАЖНО: Файловая система Render временная. Данные будут сбрасываться при перезапуске.
# Для постоянного хранения лучше использовать базу данных (например, бесплатный тариф Render Postgres).
# Но для начала оставим как есть.
IMAGES_DIR = "images"
CHANNELS_FILE = "channels.json"
SENT_IMAGES_FILE = "sent_images.json"
USERS_FILE = "users.json"

# --- Настройка логирования ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Функции для работы с данными ---
def load_data(filename: str) -> list:
    if not os.path.exists(filename):
        return []
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return []

def save_data(filename: str, data: list):
    # Используем dict.fromkeys для сохранения уникальности и порядка, затем конвертируем обратно в list
    unique_data = list(dict.fromkeys(data))
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(unique_data, f, indent=4, ensure_ascii=False)

# --- Основная логика бота для постинга ---
async def post_image_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Запуск основной задачи по отправке изображений.")
    sent_images = load_data(SENT_IMAGES_FILE)
    try:
        if not os.path.exists(IMAGES_DIR):
            os.makedirs(IMAGES_DIR)
        available_images = [f for f in os.listdir(IMAGES_DIR) if os.path.isfile(os.path.join(IMAGES_DIR, f))]
    except Exception as e:
        logger.error(f"Ошибка при доступе к папке '{IMAGES_DIR}': {e}")
        return

    unsent_images = list(set(available_images) - set(sent_images))
    if not unsent_images:
        logger.warning("Все изображения закончились.")
        await context.bot.send_message(chat_id=ADMIN_USER_ID, text="Внимание! Все изображения закончились.")
        return

    image_to_send_name = random.choice(unsent_images)
    image_path = os.path.join(IMAGES_DIR, image_to_send_name)
    logger.info(f"Выбрано изображение для рассылки: {image_to_send_name}")

    channels = load_data(CHANNELS_FILE)
    successful_sends = 0
    if channels:
        for channel_id in channels:
            try:
                with open(image_path, 'rb') as photo_file:
                    await context.bot.send_photo(chat_id=channel_id, photo=photo_file)
                successful_sends += 1
                await asyncio.sleep(0.1) # Небольшая задержка
            except Exception as e:
                logger.error(f"Не удалось отправить в канал {channel_id}: {e}")

    user_ids = load_data(USERS_FILE)
    if user_ids:
        for user_id in user_ids:
            try:
                with open(image_path, 'rb') as photo_file:
                    await context.bot.send_photo(chat_id=user_id, photo=photo_file)
                successful_sends += 1
                await asyncio.sleep(0.1) # Небольшая задержка
            except Exception as e:
                logger.warning(f"Не удалось отправить пользователю {user_id}: {e}")

    if successful_sends > 0:
        sent_images.append(image_to_send_name)
        save_data(SENT_IMAGES_FILE, sent_images)
        logger.info(f"Изображение {image_to_send_name} помечено как отправленное.")

# --- Команды и обработчики (остаются без изменений) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    all_users = load_data(USERS_FILE)

    # Общая логика для всех пользователей
    if user.id not in all_users:
        all_users.append(user.id)
        save_data(USERS_FILE, all_users)
        logger.info(f"Новый подписчик: {user.first_name} (ID: {user.id})")
        await update.message.reply_text("Привет! Вы подписались на ежедневную рассылку картинок.\nЧтобы отписаться, в любой момент используйте команду /stop.")
    else:
        await update.message.reply_text("Вы уже подписаны на рассылку.")

    # Отдельное сообщение ТОЛЬКО для администратора с правильным форматированием
    if user.id == ADMIN_USER_ID:
        admin_text = (
            "<b>Админ-панель:</b>\n\n"
            "<code>/addchannel @имя_канала</code>\n"
            "<code>/removechannel @имя_канала</code>\n"
            "<code>/listchannels</code>\n"
            "<code>/forcepost</code> - разослать картинку сейчас\n"
            "<code>/listusers</code> - показать всех подписчиков"
        )
        # Используем parse_mode='HTML'
        await update.message.reply_text(admin_text, parse_mode='HTML')
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_ids = load_data(USERS_FILE)
    if user_id in user_ids:
        user_ids.remove(user_id)
        save_data(USERS_FILE, user_ids)
        logger.info(f"Пользователь {user_id} отписался от рассылки.")
        await update.message.reply_text("Вы успешно отписались от рассылки.")
    else:
        await update.message.reply_text("Вы и не были подписаны.")

async def save_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(IMAGES_DIR):
        os.makedirs(IMAGES_DIR)
    photo = update.message.photo[-1]
    file_id = photo.file_id
    file_name = f"{file_id}.jpg"
    file_path = os.path.join(IMAGES_DIR, file_name)
    if os.path.exists(file_path):
        await update.message.reply_text("Эта картинка уже есть в базе.")
        return
    try:
        new_file = await photo.get_file()
        await new_file.download_to_drive(file_path)
        logger.info(f"Администратор добавил новую картинку: {file_name}")
        await update.message.reply_text("Картинка успешно сохранена в базу!")
    except Exception as e:
        logger.error(f"Не удалось сохранить картинку: {e}")
        await update.message.reply_text(f"Произошла ошибка при сохранении: {e}")

async def unauthorized_user_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Извините, эта команда только для моего администратора.")

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await unauthorized_user_reply(update, context)
        return
    try:
        channel_id = context.args[0]
        channels = load_data(CHANNELS_FILE)
        if channel_id not in channels:
            channels.append(channel_id)
            save_data(CHANNELS_FILE, channels)
            await update.message.reply_text(f"Канал {channel_id} успешно добавлен.")
        else:
            await update.message.reply_text(f"Канал {channel_id} уже есть в списке.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /addchannel @имя_канала")

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await unauthorized_user_reply(update, context)
        return
    try:
        channel_id = context.args[0]
        channels = load_data(CHANNELS_FILE)
        if channel_id in channels:
            channels.remove(channel_id)
            save_data(CHANNELS_FILE, channels)
            await update.message.reply_text(f"Канал {channel_id} удален.")
        else:
            await update.message.reply_text(f"Канала {channel_id} нет в списке.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /removechannel @имя_канала")

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await unauthorized_user_reply(update, context)
        return
    channels = load_data(CHANNELS_FILE)
    message = "Каналы для постинга:\n" + "\n".join(channels) if channels else "Список каналов пуст."
    await update.message.reply_text(message)

async def force_post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await unauthorized_user_reply(update, context)
        return
    await update.message.reply_text("Принудительно запускаю рассылку...")
    # Запускаем задачу в фоне, чтобы не блокировать ответ пользователю
    context.application.create_task(post_image_job(context), update=update)


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await unauthorized_user_reply(update, context)
        return
    user_ids = load_data(USERS_FILE)
    if not user_ids:
        await update.message.reply_text("Пока нет ни одного подписчика.")
        return
    message = f"Список подписчиков (всего {len(user_ids)}):\n\n" + "\n".join([str(uid) for uid in user_ids])
    await update.message.reply_text(message)

async def post_init(application: Application):
    """Задача, которая выполняется один раз после запуска приложения."""
    # Устанавливаем вебхук
    await application.bot.set_webhook(
        url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        allowed_updates=Update.ALL_TYPES
    )
    logger.info(f"Вебхук установлен на {WEBHOOK_URL}/{BOT_TOKEN}")
    # Создаем директории, если их нет
    if not os.path.exists(IMAGES_DIR):
        os.makedirs(IMAGES_DIR)
        logger.info(f"Создана директория {IMAGES_DIR}")

def main() -> None:
    """Основная функция для запуска бота."""
    # Создаем объект приложения
    ptb_app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # --- Регистрируем обработчики ---
    # Команды
    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(CommandHandler("stop", stop))
    # Админские команды
    ptb_app.add_handler(CommandHandler("addchannel", add_channel))
    ptb_app.add_handler(CommandHandler("removechannel", remove_channel))
    ptb_app.add_handler(CommandHandler("listchannels", list_channels))
    ptb_app.add_handler(CommandHandler("listusers", list_users))
    ptb_app.add_handler(CommandHandler("forcepost", force_post_command))
    # Обработчики сообщений
    ptb_app.add_handler(MessageHandler(filters.PHOTO & filters.User(user_id=ADMIN_USER_ID) & ~filters.COMMAND, save_photo_handler))
    # Этот обработчик лучше убрать, чтобы бот не отвечал на любой текст командой start
    # ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, start))

    # --- Настраиваем планировщик задач ---
    job_queue = ptb_app.job_queue
    if job_queue:
        # Запускать каждый час, первая отправка через 10 секунд после старта
        job_queue.run_repeating(post_image_job, interval=3600, first=10)

    # --- Запускаем бота ---
    # Эта функция запустит встроенный веб-сервер, установит вебхук и будет обрабатывать обновления
    ptb_app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        secret_token=None, # Можно добавить для безопасности
        webhook_url=f"{WEBHOOK_URL}" # Базовый URL, полный URL будет собран автоматически
    )

if __name__ == "__main__":
    main()

