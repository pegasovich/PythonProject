# -*- coding: utf-8 -*-

import asyncio
import os
import re
import json
import shutil
from datetime import datetime
from dotenv import load_dotenv

from playwright.async_api import async_playwright, Playwright, TimeoutError as PlaywrightTimeoutError, expect

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.error import TelegramError
import random
import traceback

# --- КОНФИГУРАЦИЯ ---
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv('DEV_BOT_TOKEN') # Используйте ваш токен для разработки
WORKER_COUNT = 2  # Количество одновременных работников (сессий Playwright)
BASE_SESSIONS_DIR = 'sessions_data'
BASE_SCREENSHOTS_DIR = 'screenshots_data'
BASE_KEYS_DIR = 'user_keys'
USER_DATA_ARCHIVES_DIR = 'user_data_archives'

# --- Класс для специального исключения при сбросе сессии ---
class SessionResetException(Exception):
    """Исключение, вызываемое при сбросе сессии на сайте."""
    pass

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
task_queue = asyncio.Queue()
user_sessions = {} # Словарь для хранения данных и состояний каждого пользователя

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def print_timestamped(message, worker_id=None, chat_id=None):
    """Выводит сообщение в консоль с метками времени, ID воркера и ID чата."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    worker_info = f" [Воркер-{worker_id}]" if worker_id else ""
    chat_info = f" [Чат-{chat_id}]" if chat_id else ""
    print(f"[{timestamp}]{worker_info}{chat_info} {message}")

async def save_screenshot(page, name, chat_id, locator=None, full_page=False):
    """Сохраняет скриншот и возвращает путь к нему."""
    screenshots_dir = os.path.join(BASE_SCREENSHOTS_DIR, str(chat_id))
    os.makedirs(screenshots_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(screenshots_dir, f"{name}_{timestamp}.png")
    try:
        if page.is_closed():
            print_timestamped(f"Не удалось сохранить скриншот '{name}': страница уже закрыта.", chat_id=chat_id)
            return None
        if locator:
            await locator.screenshot(path=path)
        else:
            await page.screenshot(path=path, full_page=full_page)
        print_timestamped(f"Скриншот сохранен: {path}", chat_id=chat_id)
        return path
    except Exception as e:
        print_timestamped(f"Ошибка при сохранении скриншота '{name}': {e}", chat_id=chat_id)
        return None

async def send_telegram_photo(bot: Bot, chat_id: int, photo_path: str, caption=""):
    """Отправляет фото и удаляет его после отправки."""
    if not photo_path or not os.path.exists(photo_path):
        return
    try:
        with open(photo_path, 'rb') as photo_file:
            await bot.send_photo(chat_id=chat_id, photo=photo_file, caption=caption)
        print_timestamped(f"Скриншот '{photo_path}' успешно отправлен.", chat_id=chat_id)
        os.remove(photo_path)
    except Exception as e:
        print_timestamped(f"Ошибка при отправке фото '{photo_path}': {e}", chat_id=chat_id)

# --- ЛОГИКА PLAYWRIGHT (Перенесено из SlotVisitHelp.py и адаптировано) ---


# --- ЛОГИКА PLAYWRIGHT (ФИНАЛЬНАЯ ВЕРСИЯ С НАСТРОЙКАМИ ИЗ СТАРОГО СКРИПТА) ---

async def run_booking_session(bot: Bot, chat_id: int, user_data: dict, worker_id: int):
    """
    Основная функция, выполняющая всю логику с браузером, с настройками из SlotVisitHelp.py
    """
    browser = None
    context = None
    
    storage_state_path = os.path.join(BASE_SESSIONS_DIR, f"session_{chat_id}.json")
    print_log = lambda msg: print_timestamped(msg, worker_id, chat_id)
    
    # Конфигурация, взятая из вашего рабочего скрипта
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    
    async with async_playwright() as playwright:
        try:
            # --- ЗАПУСК БРАУЗЕРА: ТОЧНО КАК В СТАРОМ СКРИПТЕ ---
            browser = await playwright.chromium.launch(
                headless=False,
                channel="chrome",
                args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
                timeout=60000
            )
            
            # --- СОЗДАНИЕ КОНТЕКСТА: ТОЧНО КАК В СТАРОМ СКРИПТЕ ---
            if os.path.exists(storage_state_path):
                print_log("Найден файл сессии. Пытаюсь загрузить...")
                context = await browser.new_context(
                    storage_state=storage_state_path,
                    locale="uk-UA",
                    user_agent=USER_AGENT,
                    timezone_id="Europe/Kiev"
                )
            else:
                print_log("Файл сессии не найден. Будет создана новая.")
                context = await browser.new_context(
                    locale="uk-UA",
                    user_agent=USER_AGENT,
                    timezone_id="Europe/Kiev"
                )
            
            page = await context.new_page()

          
    
            await page.evaluate("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})") # Метод 2: Ваш ручной патч

            # Дальнейший код остается без изменений...
            
            await page.goto("https://e-consul.gov.ua/", timeout=60000)
            print_log(f"Перешел на главную страницу. URL: {page.url}")

            # Проверка, активна ли сессия
            is_logged_in = "id.e-consul" not in page.url
            if is_logged_in:
                 print_log("Сессия активна. Пропускаю авторизацию.")
                 await bot.send_message(chat_id, "Сессия активна. Пропускаю шаг авторизации.")
            else:
                print_log("Сессия неактивна. Начинаю процесс авторизации.")
                await bot.send_message(chat_id, "Начинаю процесс авторизации...")
                # --- Логика Авторизации ---
                auth_method = user_data.get('auth_method')
                
                if auth_method == 'key':
                    print_log("Выбран метод 'Особистий ключ'.")
                    await page.get_by_role("button", name="Особистий ключ").click()
                    await page.wait_for_load_state('networkidle')
                    
                    async with page.expect_file_chooser() as fc_info:
                        await page.locator('text="Оберіть ключ на своєму носієві"').click()
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(user_data['key_file_path'])
                    print_log(f"Файл ключа '{user_data['key_file_path']}' выбран.")
                    
                    await page.locator('#id-app-login-sign-form-file-key-server').click()
                    await page.locator('li[data-value="31"]').click()
                    print_log("Выбран КНЕДП monobank.")

                    await page.locator('input#id-app-login-sign-form-file-key-password').fill(user_data['key_password'])
                    print_log("Пароль введен.")
                    
                    async with page.expect_navigation(url="**/e-consul.gov.ua/**"):
                        await page.locator('button#id-app-login-sign-form-file-key-sign-button').click()
                    
                    await page.wait_for_load_state('networkidle')
                    print_log(f"Авторизация по ключу прошла успешно. URL: {page.url}")
                    await bot.send_message(chat_id, "✅ Авторизация по ключу прошла успешно.")

            # Сохраняем состояние сессии для будущих запусков
            await context.storage_state(path=storage_state_path)
            print_log(f"Состояние сессии сохранено в {storage_state_path}")
            
            # --- НАВИГАЦИЯ К ЗАПИСИ ---
            await bot.send_message(chat_id, "Перехожу к созданию записи...")
            await page.get_by_role("link", name="Запис на візит").click()
            await page.get_by_role("link", name="Записатись на візит").click()
            await page.wait_for_load_state('networkidle')
            print_log("Перешел на страницу создания записи.")

            # --- ПОИСК СЛОТОВ ---
            await bot.send_message(chat_id, "Начинаю поиск свободных слотов. Это может занять много времени...")
            print_log("Начинаю цикл поиска слотов.")
            
            no_slots_locator = page.locator('text="На жаль, зараз немає вільного часу"')
            
            max_retries = 99999
            for i in range(max_retries):
                print_log(f"Попытка поиска слотов #{i + 1}")
                await page.wait_for_timeout(3000)
                
                if not await no_slots_locator.is_visible():
                    print_log("!!! Найдена неделя со свободными слотами !!!")
                    await bot.send_message(chat_id, "🔥 **НАЙДЕНЫ СЛОТЫ!** 🔥\nНачинаю бронирование...")
                    
                    available_day = page.locator('.day-wrapper.available').first
                    day_text = await available_day.text_content()
                    await available_day.click()
                    print_log(f"Выбран доступный день: {day_text}")

                    await page.wait_for_timeout(2000)
                    
                    available_time = page.locator('.time-slot.available').first
                    time_text = await available_time.text_content()
                    await available_time.click()
                    print_log(f"Выбрано доступное время: {time_text}")
                    
                    screenshot_path = await save_screenshot(page, "final_confirmation", chat_id)
                    await send_telegram_photo(bot, chat_id, screenshot_path, caption=f"Подтверждение записи на {day_text}, {time_text}")
                    
                    await page.get_by_role("button", name="Записатись на прийом").click()

                    success_locator = page.locator('text="Ваш візит успішно заброньовано"')
                    await expect(success_locator).to_be_visible(timeout=60000)
                    
                    print_log("!!! БРОНИРОВАНИЕ УСПЕШНО ЗАВЕРШЕНО !!!")
                    final_msg = f"🎉 **УСПЕХ!** 🎉\nВизит на {day_text}, {time_text} успешно забронирован!"
                    await bot.send_message(chat_id, final_msg)
                    
                    final_screenshot = await save_screenshot(page, "booking_confirmed", chat_id)
                    await send_telegram_photo(bot, chat_id, final_screenshot, caption="Подтверждение!")
                    
                    return
                
                else:
                    print_log("Свободных слотов нет. Перезагружаю страницу...")
                    await page.reload(wait_until='networkidle')
                    await asyncio.sleep(random.randint(10, 20))
                    
            await bot.send_message(chat_id, "Достигнут лимит попыток, а слоты так и не найдены. Остановка.")

        except Exception as e:
            error_message = f"Произошла критическая ошибка: {e}"
            print_log(error_message)
            full_traceback = traceback.format_exc()
            print_log("\nПОЛНЫЙ ТРЕЙСБЕК ОШИБКИ:\n" + full_traceback)
            await bot.send_message(chat_id, f"❗️ **Произошла ошибка в работе воркера.**\n\n`{e}`\n\nЗадача остановлена.")
            if 'page' in locals() and page and not page.is_closed():
                error_screenshot = await save_screenshot(page, "error_screenshot", chat_id)
                await send_telegram_photo(bot, chat_id, error_screenshot, caption="Скриншот на момент ошибки")

        finally:
            if browser:
                await browser.close()
            print_log("Сессия Playwright завершена.")


# --- WORKER ---
async def worker(worker_id: int, app: Application):
    """
    Воркер - это вечный процесс, который ждет задачи из очереди и выполняет их.
    """
    print_timestamped(f"Воркер {worker_id} запущен и готов к работе.", worker_id)
    while True:
        try:
            chat_id = await task_queue.get()
            
            print_timestamped(f"Взял в работу пользователя {chat_id}.", worker_id)
            
            user_data = user_sessions.get(chat_id)
            if not user_data:
                print_timestamped(f"Данные для {chat_id} не найдены, задача пропущена.", worker_id)
                task_queue.task_done()
                continue
            
            # Запускаем основную логику в этой функции
            await run_booking_session(app.bot, chat_id, user_data, worker_id)

            print_timestamped(f"Завершил работу с пользователем {chat_id}.", worker_id)
            # Удаляем сессию пользователя после завершения
            if chat_id in user_sessions:
                 del user_sessions[chat_id]
            task_queue.task_done()

        except Exception as e:
            print_timestamped(f"Критическая ошибка в воркере {worker_id}: {e}", worker_id)


# --- ЛОГИКА ДИАЛОГА С ПОЛЬЗОВАТЕЛЕМ (ConversationHandler) ---

# Определяем состояния нашего диалога
(SELECTING_AUTH, AWAITING_KEY_FILE, AWAITING_PASSWORD) = range(3)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start."""
    await update.message.reply_text(
        "👋 **Привет!**\n\n"
        "Я бот для автоматической записи на консульские услуги.\n"
        "Я буду искать свободные слоты 24/7 и попытаюсь записать вас, как только появится возможность.\n\n"
        "Чтобы начать настройку и встать в очередь, используйте команду /book."
    )

async def book_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает диалог сбора данных."""
    chat_id = update.effective_chat.id
    
    # Проверяем, нет ли уже активной задачи для этого пользователя
    if chat_id in list(task_queue._queue):
        await update.message.reply_text("❗️ Ваша задача уже находится в очереди на выполнение. Чтобы начать заново, отмените старую задачу командой /cancel.")
        return ConversationHandler.END

    if chat_id in user_sessions and 'key_password' in user_sessions[chat_id]:
         await update.message.reply_text("❗️ У вас уже есть активная сессия настройки. Чтобы начать заново, используйте /cancel.")
         return ConversationHandler.END

    # Создаем чистую сессию для пользователя
    user_sessions[chat_id] = {}
    
    keyboard = [
        # [InlineKeyboardButton("Дія.Підпис", callback_data='diia')],
        [InlineKeyboardButton("Особистий ключ (рекомендується)", callback_data='key')],
        # [InlineKeyboardButton("ID.GOV.UA (BankID)", callback_data='idgov')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "**Шаг 1 из 3: Метод входа**\n\n"
        "Пожалуйста, выберите метод аутентификации на сайте e-consul.gov.ua.",
        reply_markup=reply_markup
    )
    
    return SELECTING_AUTH

async def select_auth_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор метода аутентификации."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    
    auth_method = query.data
    user_sessions[chat_id]['auth_method'] = auth_method
    
    await query.edit_message_text(text=f"Выбран метод: **{auth_method.upper()}**.")

    if auth_method == 'key':
        await context.bot.send_message(
            chat_id, 
            "**Шаг 2 из 3: Файл ключа**\n\n"
            "Отлично. Теперь, пожалуйста, прикрепите и отправьте мне ваш файл ключа "
            "(обычно это файл с расширением `.dat`, `.pfx`, `.jks`, `.zs2`)."
        )
        return AWAITING_KEY_FILE
    else:
        # Логика для других методов
        await context.bot.send_message(chat_id, "Этот метод пока не поддерживается. Пожалуйста, перезапустите командой /book и выберите 'Особистий ключ'.")
        return ConversationHandler.END

async def receive_key_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает файл ключа и запрашивает пароль."""
    chat_id = update.effective_chat.id
    document = update.message.document
    
    # Создаем уникальную папку для ключей этого пользователя
    keys_dir = os.path.join(BASE_KEYS_DIR, str(chat_id))
    os.makedirs(keys_dir, exist_ok=True)
    
    file_path = os.path.join(keys_dir, document.file_name)
    
    # Скачиваем файл
    file = await document.get_file()
    await file.download_to_drive(file_path)
    
    user_sessions[chat_id]['key_file_path'] = file_path
    print_timestamped(f"Файл ключа для {chat_id} сохранен в {file_path}", chat_id=chat_id)
    
    await update.message.reply_text(
        "**Шаг 3 из 3: Пароль**\n\n"
        "✅ Файл получен. Теперь введите пароль от этого ключа.\n\n"
        "_(Сообщение с паролем будет удалено сразу после отправки для вашей безопасности)_"
    )
    
    return AWAITING_PASSWORD

async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает пароль, завершает сбор данных и ставит задачу в очередь."""
    chat_id = update.effective_chat.id
    password = update.message.text
    user_sessions[chat_id]['key_password'] = password
    
    # Удаляем сообщение с паролем для безопасности
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        await update.message.reply_text("Пароль получен и удален.")
    except Exception as e:
        print_timestamped(f"Не удалось удалить сообщение с паролем для {chat_id}: {e}", chat_id=chat_id)
        
    await context.bot.send_message(
        chat_id, 
        "🎉 **Настройка завершена!**\n\n"
        "Все данные собраны. Я добавляю вашу задачу в очередь на выполнение.\n"
        "Как только один из воркеров освободится, он возьмет вашу задачу в работу.\n\n"
        "Вы можете проверить статус очереди командой /status."
    )
    
    # Все данные собраны, добавляем задачу в очередь
    await task_queue.put(chat_id)
    print_timestamped(f"Пользователь {chat_id} добавлен в очередь.", chat_id=chat_id)
        
    return ConversationHandler.END

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает статус очереди."""
    queue_size = task_queue.qsize()
    # Проверяем, есть ли сам пользователь в очереди
    try:
        # Преобразуем очередь в список для поиска
        current_queue_list = list(task_queue._queue)
        user_position = current_queue_list.index(update.effective_chat.id) + 1
        position_text = f"Ваша позиция в очереди: **{user_position}**."
    except ValueError:
        position_text = "Вашей задачи нет в очереди. Возможно, она уже выполняется или была завершена."

    await update.message.reply_text(
        f"Текущий статус:\n\n"
        f"⚙️ Работающих воркеров: **{WORKER_COUNT}**\n"
        f"👥 Задач в очереди: **{queue_size}**\n\n"
        f"{position_text}"
    )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменяет и завершает диалог, удаляет данные пользователя."""
    chat_id = update.effective_chat.id
    if chat_id in user_sessions:
        del user_sessions[chat_id]
        print_timestamped(f"Сессия для {chat_id} удалена.", chat_id=chat_id)

    # Попытка удалить задачу из очереди, если она там есть
    if chat_id in list(task_queue._queue):
        new_queue = asyncio.Queue()
        while not task_queue.empty():
            item = await task_queue.get()
            if item != chat_id:
                await new_queue.put(item)
        
        # Заменяем старую очередь на новую
        globals()['task_queue'] = new_queue
        print_timestamped(f"Задача для {chat_id} удалена из очереди.", chat_id=chat_id)
        
    await update.message.reply_text("Все ваши данные и активные задачи были удалены. Вы можете начать заново с /book.")
    return ConversationHandler.END

# --- ЗАПУСК БОТА И ВОРКЕРОВ ---

async def post_init(application: Application) -> None:
    """Создает и запускает воркеров после инициализации бота."""
    # Создаем нужные директории при старте
    os.makedirs(BASE_SESSIONS_DIR, exist_ok=True)
    os.makedirs(BASE_SCREENSHOTS_DIR, exist_ok=True)
    os.makedirs(BASE_KEYS_DIR, exist_ok=True)

    for i in range(WORKER_COUNT):
        asyncio.create_task(worker(worker_id=i + 1, app=application))

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловит все ошибки и выводит их в консоль."""
    print_timestamped(f"Произошла ошибка в обработчике telegram: {context.error}")


def main() -> None:
    """Основная функция запуска бота."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("book", book_command)],
        states={
            # Убираем per_message=True отсюда
            SELECTING_AUTH: [CallbackQueryHandler(select_auth_method)],
            AWAITING_KEY_FILE: [MessageHandler(filters.Document.ALL, receive_key_file)],
            AWAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        # И меняем per_user на False, чтобы решить проблему с предупреждением
        per_user=False,
        per_chat=True,
    )
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(conv_handler)
    
    application.add_error_handler(error_handler)
    
    print_timestamped(f"Бот запускается с {WORKER_COUNT} воркерами...")
    application.run_polling()

if __name__ == "__main__":
    main()