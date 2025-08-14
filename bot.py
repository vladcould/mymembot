import os
import random
import json
import logging
import time
import threading # Добавляем для фоновой работы планировщика
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from flask import Flask, request # Добавляем веб-фреймворк Flask

# --- КОНФИГУРАЦИЯ ---
# ИЗМЕНЕНИЕ: Теперь мы берем данные из "окружения" сервера, а не из кода
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID"))
# Имя вашего будущего веб-сервиса на Render (например, my-poster-bot.onrender.com)
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

# --- Названия файлов для хранения данных ---
IMAGES_DIR = "images"
CHANNELS_FILE = "channels.json"
SENT_IMAGES_FILE = "sent_images.json"
USERS_FILE = "users.json"

# --- Настройка логирования ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Инициализация Flask-приложения ---
app = Flask(__name__)

# --- ВЕСЬ ВАШ ПРЕДЫДУЩИЙ КОД (ФУНКЦИИ) ОСТАЕТСЯ ЗДЕСЬ БЕЗ ИЗМЕНЕНИЙ ---
# ... (load_data, save_data, post_image_job, start, stop, save_photo_handler и все остальные)
def load_data(filename: str) -> list:
    if not os.path.exists(filename): return []
    try:
        with open(filename, 'r', encoding='utf-8') as f: return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError): return []
def save_data(filename: str, data: list):
    with open(filename, 'w', encoding='utf-8') as f:
        unique_data = list(set(data)); json.dump(unique_data, f, indent=4)
async def post_image_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Запуск основной задачи по отправке изображений.")
    sent_images = load_data(SENT_IMAGES_FILE)
    try:
        if not os.path.exists(IMAGES_DIR): os.makedirs(IMAGES_DIR)
        available_images = [f for f in os.listdir(IMAGES_DIR) if os.path.isfile(os.path.join(IMAGES_DIR, f))]
    except FileNotFoundError: logger.error(f"Папка '{IMAGES_DIR}' не найдена!"); return
    unsent_images = list(set(available_images) - set(sent_images))
    if not unsent_images:
        logger.warning("Все изображения закончились."); await context.bot.send_message(chat_id=ADMIN_USER_ID, text="Внимание! Все изображения закончились."); return
    image_to_send_name = random.choice(unsent_images); image_path = os.path.join(IMAGES_DIR, image_to_send_name)
    logger.info(f"Выбрано изображение для рассылки: {image_to_send_name}")
    channels = load_data(CHANNELS_FILE); successful_channel_sends = 0
    if channels:
        for channel_id in channels:
            try:
                with open(image_path, 'rb') as photo_file: await context.bot.send_photo(chat_id=channel_id, photo=photo_file)
                successful_channel_sends += 1
            except Exception as e: logger.error(f"Не удалось отправить в канал {channel_id}: {e}")
    user_ids = load_data(USERS_FILE); successful_user_sends = 0
    if user_ids:
        for user_id in user_ids:
            try:
                with open(image_path, 'rb') as photo_file: await context.bot.send_photo(chat_id=user_id, photo=photo_file)
                successful_user_sends += 1; time.sleep(0.1)
            except Exception as e: logger.warning(f"Не удалось отправить пользователю {user_id}: {e}")
    if successful_channel_sends > 0 or successful_user_sends > 0:
        sent_images.append(image_to_send_name); save_data(SENT_IMAGES_FILE, sent_images)
        logger.info(f"Изображение {image_to_send_name} помечено как отправленное.")
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; all_users = load_data(USERS_FILE)
    if user.id not in all_users:
        all_users.append(user.id); save_data(USERS_FILE, all_users); logger.info(f"Новый подписчик: {user.first_name} (ID: {user.id})")
        await update.message.reply_text("Привет! Вы подписались на ежедневную рассылку картинок.\nЧтобы отписаться, в любой момент используйте команду /stop.")
    else: await update.message.reply_text("Вы уже подписаны на рассылку.")
    if user.id == ADMIN_USER_ID:
        await update.message.reply_text("<b>Админ-панель:</b>\n\n`/addchannel @имя_канала`\n`/removechannel @имя_канала`\n`/listchannels`\n`/forcepost` - разослать картинку сейчас\n`/listusers` - показать всех подписчиков", parse_mode='Markdown')
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; user_ids = load_data(USERS_FILE)
    if user_id in user_ids:
        user_ids.remove(user_id); save_data(USERS_FILE, user_ids); logger.info(f"Пользователь {user_id} отписался от рассылки.")
        await update.message.reply_text("Вы успешно отписались от рассылки.")
    else: await update.message.reply_text("Вы и не были подписаны.")
async def save_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]; file_id = photo.file_id; file_name = f"{file_id}.jpg"; file_path = os.path.join(IMAGES_DIR, file_name)
    if os.path.exists(file_path): await update.message.reply_text("Эта картинка уже есть в базе."); return
    try:
        new_file = await photo.get_file(); await new_file.download_to_drive(file_path)
        logger.info(f"Администратор добавил новую картинку: {file_name}"); await update.message.reply_text("Картинка успешно сохранена в базу!")
    except Exception as e: logger.error(f"Не удалось сохранить картинку: {e}"); await update.message.reply_text(f"Произошла ошибка при сохранении: {e}")
async def unauthorized_user_reply(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("Извините, эта команда только для моего администратора.")
async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    try:
        channel_id = context.args[0]; channels = load_data(CHANNELS_FILE)
        if channel_id not in channels: channels.append(channel_id); save_data(CHANNELS_FILE, channels); await update.message.reply_text(f"Канал {channel_id} успешно добавлен.")
        else: await update.message.reply_text(f"Канал {channel_id} уже есть в списке.")
    except (IndexError, ValueError): await update.message.reply_text("Использование: /addchannel @имя_канала")
async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    try:
        channel_id = context.args[0]; channels = load_data(CHANNELS_FILE)
        if channel_id in channels: channels.remove(channel_id); save_data(CHANNELS_FILE, channels); await update.message.reply_text(f"Канал {channel_id} удален.")
        else: await update.message.reply_text(f"Канала {channel_id} нет в списке.")
    except (IndexError, ValueError): await update.message.reply_text("Использование: /removechannel @имя_канала")
async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    channels = load_data(CHANNELS_FILE); message = "Каналы для постинга:\n" + "\n".join(channels) if channels else "Список каналов пуст."; await update.message.reply_text(message)
async def force_post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    await update.message.reply_text("Принудительно запускаю рассылку..."); await post_image_job(context)
async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID: await unauthorized_user_reply(update, context); return
    user_ids = load_data(USERS_FILE)
    if not user_ids: await update.message.reply_text("Пока нет ни одного подписчика."); return
    message = f"Список подписчиков (всего {len(user_ids)}):\n\n" + "\n".join([str(uid) for uid in user_ids])
    await update.message.reply_text(message)

# --- НОВЫЙ БЛОК: Инициализация и запуск бота ---

# Создаем объект приложения один раз
ptb_app = Application.builder().token(BOT_TOKEN).build()

# Добавляем все обработчики
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("stop", stop))
ptb_app.add_handler(CommandHandler("addchannel", add_channel))
ptb_app.add_handler(CommandHandler("removechannel", remove_channel))
ptb_app.add_handler(CommandHandler("listchannels", list_channels))
ptb_app.add_handler(CommandHandler("listusers", list_users))
ptb_app.add_handler(CommandHandler("forcepost", force_post_command))
ptb_app.add_handler(MessageHandler(filters.PHOTO & filters.User(user_id=ADMIN_USER_ID) & ~filters.COMMAND, save_photo_handler))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, start))

# Настраиваем планировщик задач
job_queue = ptb_app.job_queue
if job_queue:
    job_queue.run_repeating(post_image_job, interval=3600, first=10)

async def start_bot_and_scheduler():
    """Запускает планировщик задач в фоновом потоке."""
    await ptb_app.initialize()
    await ptb_app.start()
    # Запускаем планировщик в фоновом режиме
    if ptb_app.job_queue:
        await ptb_app.job_queue.start()
    await ptb_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    logger.info("Бот и планировщик запущены, вебхук установлен.")

# --- Flask веб-сервер ---

@app.route("/")
def index():
    """Пустая главная страница, чтобы 'пингер' мог ее проверять."""
    return "Bot is running!", 200

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook_handler():
    """Принимает обновления от Telegram и передает их боту."""
    # Используем threading, чтобы не блокировать веб-сервер
    threading.Thread(target=process_update_sync).start()
    return 'ok', 200

def process_update_sync():
    """Синхронная обертка для асинхронной обработки."""
    import asyncio
    update_data = request.get_json(force=True)
    update = Update.de_json(data=update_data, bot=ptb_app.bot)
    
    # Запускаем асинхронную обработку в новом цикле событий
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(ptb_app.process_update(update))
    loop.close()

if __name__ == "__main__":
    # Запускаем асинхронную функцию для инициализации бота
    import asyncio
    asyncio.run(start_bot_and_scheduler())
    
    # Запускаем веб-сервер Flask
    # Render сам выберет порт, поэтому мы используем os.environ.get('PORT', 8000)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)))
