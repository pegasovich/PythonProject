import asyncio
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
# Добавьте или исправьте эти строки в начале файла

from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
# ... другие импорты
from playwright.async_api import async_playwright, Playwright


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






# --- НОВАЯ ФУНКЦИЯ ДЛЯ PLAYWRIGHT ---
async def run_booking_session(playwright: Playwright, bot: Application, chat_id: int, user_data: dict):
    """
    Запускает сессию Playwright для выполнения задачи бронирования.
    """
    browser = None # Инициализируем переменную browser
    try:
        print_timestamped(f"Playwright: Запускаю браузер для пользователя {chat_id}...")
        
        # Запускаем браузер Chromium. headless=False позволит видеть окно браузера.
        # Для работы на сервере установите headless=True.
        browser = await playwright.chromium.launch(headless=False) 
        page = await browser.new_page()

        # --- НАЧАЛО ЛОГИКИ АВТОМАТИЗАЦИИ ---
        
        # 1. Переходим на сайт
        await bot.bot.send_message(chat_id, "Начинаю работу. Перехожу на сайт...")
        await page.goto("https://infocons.mfa.gov.ua/login") # ЗАМЕНИТЕ НА ВАШ URL
        await page.wait_for_load_state('networkidle') # Ждем полной прогрузки страницы
        
        print_timestamped(f"Playwright: Страница загружена для {chat_id}.")

        # 2. Здесь будет логика входа в систему
        # Например, если выбран метод 'key':
        if user_data.get('auth_method') == 'key':
            await bot.bot.send_message(chat_id, "Прохожу аутентификацию с помощью ключа...")
            # await page.click('selector-for-key-auth-button')
            # await page.locator('input[type="file"]').set_input_files(user_data['key_file_path'])
            # await page.fill('selector-for-password-input', user_data['key_password'])
            # await page.click('selector-for-login-button')
            print_timestamped(f"Playwright: Попытка входа с ключом для {chat_id}.")

        # 3. Здесь будет логика заполнения форм, поиска слотов и т.д.
        # ...

        await asyncio.sleep(15) # Временная пауза для демонстрации
        
        # --- КОНЕЦ ЛОГИКИ АВТОМАТИЗАЦИИ ---

        await bot.bot.send_message(chat_id, "✅ Задача успешно выполнена!")

    except Exception as e:
        # В случае ошибки отправляем сообщение пользователю
        error_message = f"Произошла ошибка во время выполнения вашей задачи: {e}"
        print_timestamped(error_message, chat_id)
        await bot.bot.send_message(chat_id, f"❗️ Ошибка: {e}")
    finally:
        # Убеждаемся, что браузер всегда будет закрыт
        if browser:
            await browser.close()
            print_timestamped(f"Playwright: Браузер для {chat_id} закрыт.")







# --- WORKER ---
async def worker(worker_id: int, bot: Application):
    """
    Воркер - это вечный процесс, который ждет задачи из очереди и выполняет их.
    """
    print_timestamped(f"Воркер {worker_id} запущен и готов к работе.", worker_id)
    
    # Запускаем экземпляр Playwright один раз для каждого воркера
    async with async_playwright() as playwright:
        while True:
            try:
                # Получаем задачу из очереди
                chat_id = await task_queue.get()
                print_timestamped(f"Взял в работу пользователя {chat_id}.", worker_id)

                # Получаем данные пользователя из сессии
                user_data = user_sessions.get(chat_id)
                if not user_data:
                    print_timestamped(f"Не найдены данные сессии для {chat_id}. Пропускаю.", worker_id)
                    task_queue.task_done()
                    continue

                # --- ЗАПУСКАЕМ СЕССИЮ PLAYWRIGHT ---
                # Вместо asyncio.sleep вызываем нашу новую функцию
                await bot.bot.send_message(chat_id, f"Воркер #{worker_id} приступил к выполнению вашей задачи...")
                await run_booking_session(playwright, bot, chat_id, user_data)
                # ------------------------------------

                print_timestamped(f"Завершил работу с пользователем {chat_id}.", worker_id)
                task_queue.task_done()

            except Exception as e:
                print_timestamped(f"Критическая ошибка в воркере {worker_id}: {e}", worker_id)






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

# --- НОВАЯ ФУНКЦИЯ ДЛЯ ЗАПУСКА ВОРКЕРОВ ---
async def post_init(application: Application) -> None:
    """Создает и запускает 3 воркера в фоновом режиме после инициализации бота."""
    for i in range(WORKER_COUNT):
        # Передаем application, а не application.bot, чтобы иметь доступ ко всему приложению
        asyncio.create_task(worker(worker_id=i + 1, bot=application))



async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловит все ошибки и выводит их в консоль в аккуратном виде."""
    print_timestamped(f"Произошла ошибка: {context.error}")




# --- ЛОГИКА ДИАЛОГА С ПОЛЬЗОВАТЕЛЕМ (ConversationHandler) ---

# Определяем состояния нашего диалога
(SELECTING_AUTH, AWAITING_KEY_FILE, AWAITING_PASSWORD, AWAITING_KNEDP, 
 AWAITING_GENDER, AWAITING_DOB, AWAITING_COUNTRY, AWAITING_INSTITUTION, 
 AWAITING_SERVICE, AWAITING_RECIPIENT, AWAITING_CHILD_DETAILS) = range(11)

async def book_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает диалог сбора данных."""
    chat_id = update.effective_chat.id
    # Создаем чистую сессию для пользователя
    user_sessions[chat_id] = {}
    
    keyboard = [
        [InlineKeyboardButton("Дія.Підпис", callback_data='diia')],
        [InlineKeyboardButton("Особистий ключ", callback_data='key')],
        [InlineKeyboardButton("ID.GOV.UA (BankID)", callback_data='idgov')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text("Давайте начнем. Выберите метод аутентификации:", reply_markup=reply_markup)
    
    return SELECTING_AUTH

async def select_auth_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор метода аутентификации."""
    query = update.callback_query
    await query.answer()
    chat_id = query.effective_chat.id
    
    auth_method = query.data
    user_sessions[chat_id]['auth_method'] = auth_method
    
    await query.edit_message_text(text=f"Выбран метод: {auth_method.upper()}")

    # В зависимости от метода, переходим к следующему шагу
    if auth_method == 'key':
        await context.bot.send_message(chat_id, "Теперь, пожалуйста, прикрепите ваш файл ключа (.dat, .pfx, .jks и т.д.).")
        return AWAITING_KEY_FILE
    else:
        # Для Diia и ID.GOV сбор данных закончен, можно переходить к общим вопросам
        await context.bot.send_message(chat_id, "Отлично. Теперь несколько общих вопросов.")
        await context.bot.send_message(chat_id, "Пожалуйста, введите вашу страну пребывания:")
        return AWAITING_COUNTRY # Пропускаем шаги с ключом

async def receive_key_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает файл ключа и запрашивает пароль."""
    chat_id = update.effective_chat.id
    document = update.message.document
    
    # Создаем уникальную папку для ключей этого пользователя
    keys_dir = os.path.join('user_keys', str(chat_id))
    os.makedirs(keys_dir, exist_ok=True)
    
    file_path = os.path.join(keys_dir, document.file_name)
    
    # Скачиваем файл
    file = await document.get_file()
    await file.download_to_drive(file_path)
    
    user_sessions[chat_id]['key_file_path'] = file_path
    print_timestamped(f"Файл ключа для пользователя {chat_id} сохранен в {file_path}")
    
    await update.message.reply_text("Файл получен. Теперь введите пароль от этого ключа:")
    
    return AWAITING_PASSWORD

async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает пароль и завершает сбор данных."""
    chat_id = update.effective_chat.id
    password = update.message.text
    user_sessions[chat_id]['key_password'] = password
    
    # Удаляем сообщение с паролем для безопасности
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
    except Exception as e:
        print_timestamped(f"Не удалось удалить сообщение с паролем для {chat_id}: {e}")
        
    await context.bot.send_message(chat_id, "Пароль получен.")
    await context.bot.send_message(chat_id, "Пожалуйста, введите вашу страну пребывания:")
    return AWAITING_COUNTRY

# ЗАГЛУШКИ: В будущем здесь будет логика для остальных шагов
async def receive_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    user_sessions[chat_id]['country'] = update.message.text
    
    # Тут можно добавить сложную логику с кнопками, но пока просто собираем текст
    
    print_timestamped(f"Данные для {chat_id}: {user_sessions[chat_id]}")
    await update.message.reply_text("Спасибо, все данные собраны!")
    
    # Все данные собраны, добавляем задачу в очередь
    if chat_id not in list(task_queue._queue):
        await task_queue.put(chat_id)
        await update.message.reply_text(f"✅ Ваша задача добавлена в очередь! Вы можете проверить статус командой /status.")
    else:
        await update.message.reply_text("❗️ Ваша задача уже находится в очереди.")
        
    return ConversationHandler.END # Завершаем диалог

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменяет и завершает диалог."""
    chat_id = update.effective_chat.id
    if chat_id in user_sessions:
        del user_sessions[chat_id]
        
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END




def main() -> None:
    """Запускает бота, создает пул воркеров и настраивает диалог."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # --- Создаем обработчик диалога ---
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("book", book_command)],
        states={
            SELECTING_AUTH: [CallbackQueryHandler(select_auth_method)],
            AWAITING_KEY_FILE: [MessageHandler(filters.Document.ALL, receive_key_file)],
            AWAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
            # Пока что используем одну функцию-заглушку для всех текстовых ответов
            AWAITING_COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_country)], 
            # В будущем здесь будут обработчики для AWAITING_GENDER, AWAITING_DOB и т.д.
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, # Важно для разделения диалогов между пользователями
        per_chat=True,
    )

    application.add_error_handler(error_handler)
    
    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler) # Добавляем наш новый сложный обработчик
    application.add_handler(CommandHandler("status", status))

    print_timestamped(f"Бот запускается с {WORKER_COUNT} воркерами...")
    application.run_polling()






if __name__ == "__main__":
    main()