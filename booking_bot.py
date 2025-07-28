import asyncio
import os
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- КОНФИГУРАЦИЯ ---
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv('DEV_BOT_TOKEN')
WORKER_COUNT = 3  # Количество одновременных работников

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
# Очередь, в которую будут складываться задачи для воркеров
task_queue = asyncio.Queue()
# Словарь для хранения данных и состояний каждого пользователя
user_sessions = {}

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def print_timestamped(message, worker_id=None):
    """Выводит сообщение в консоль с метками времени и ID воркера."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    worker_info = f" [Воркер-{worker_id}]" if worker_id else ""
    print(f"[{timestamp}]{worker_info} {message}")

# --- WORKER ---
async def worker(worker_id: int, bot: Application):
    """
    Воркер - это вечный процесс, который ждет задачи из очереди и выполняет их.
    """
    print_timestamped(f"Воркер {worker_id} запущен и готов к работе.", worker_id)
    while True:
        try:
            # Получаем задачу из очереди. Если очередь пуста, воркер будет "спать" здесь.
            chat_id = await task_queue.get()
            
            print_timestamped(f"Взял в работу пользователя {chat_id}.", worker_id)
            
            # --- ЗДЕСЬ БУДЕТ ЛОГИКА PLAYWRIGHT ---
            # Имитируем долгую работу
            await bot.bot.send_message(chat_id, f"Воркер #{worker_id} начал выполнять вашу задачу...")
            await asyncio.sleep(30) # В будущем здесь будет запуск браузера
            await bot.bot.send_message(chat_id, f"Воркер #{worker_id} завершил вашу задачу!")
            # ------------------------------------

            print_timestamped(f"Завершил работу с пользователем {chat_id}.", worker_id)
            task_queue.task_done()

        except Exception as e:
            print_timestamped(f"Ошибка в воркере {worker_id}: {e}", worker_id)

# --- ОБРАБОТЧИКИ КОМАНД TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_sessions[chat_id] = {'state': 'new'} # Создаем сессию для нового пользователя
    await update.message.reply_text(
        "👋 Привет! Я бот для поиска слотов. \n"
        "Чтобы добавить задачу в очередь, используйте команду /book"
    )

async def book(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    # Проверяем, нет ли этого пользователя уже в очереди
    if chat_id not in list(task_queue._queue):
        await task_queue.put(chat_id)
        await update.message.reply_text(f"✅ Ваша задача добавлена в очередь! Количество задач перед вами: {task_queue.qsize() - 1}.")
    else:
        await update.message.reply_text("❗️ Ваша задача уже находится в очереди.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    queue_size = task_queue.qsize()
    await update.message.reply_text(f"👥 Сейчас в очереди на выполнение: {queue_size} задач.")

# --- ОСНОВНАЯ ФУНКЦИЯ ЗАПУСКА ---
def main() -> None:
    """Запускает бота и создает пул воркеров."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Создаем и запускаем 3 воркера в фоновом режиме
    for i in range(WORKER_COUNT):
        asyncio.create_task(worker(worker_id=i + 1, bot=application))

    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("book", book))
    application.add_handler(CommandHandler("status", status))

    print_timestamped(f"Бот запускается с {WORKER_COUNT} воркерами...")
    application.run_polling()

if __name__ == "__main__":
    main()