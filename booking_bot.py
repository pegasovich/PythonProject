# -*- coding: utf-8 -*-

import asyncio
import os
import re
import json
import shutil
import traceback
import random
import sys
import logging
from datetime import datetime
from dotenv import load_dotenv
import calendar
import argparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, expect

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot, BotCommand,
    BotCommandScopeChat, BotCommandScopeAllPrivateChats,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
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

# --- CONFIGURATION ---
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv('DEV_BOT_TOKEN')
OWNER_TELEGRAM_ID = os.getenv('OWNER_TELEGRAM_ID')
WORKER_COUNT = 2
# Папку с сессиями переименовали для ясности
BASE_PROFILES_DIR = 'browser_profiles'
BASE_SCREENSHOTS_DIR = 'screenshots_data'
BASE_KEYS_DIR = 'user_keys'
USER_DATA_ARCHIVES_DIR = 'user_data_archives'
DATA_FILE = 'countries_data.json'

# --- GLOBAL VARIABLES ---
task_queue = asyncio.Queue()
user_sessions = {}


class SessionResetException(Exception):
    """Custom exception for session resets that require a restart."""
    pass

# --- HELPER FUNCTIONS ---

async def save_screenshot(page, name, chat_id, locator=None, full_page=False):
    screenshots_dir = os.path.join(BASE_SCREENSHOTS_DIR, str(chat_id))
    os.makedirs(screenshots_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(screenshots_dir, f"{name}_{timestamp}.png")
    try:
        if page.is_closed():
            logging.warning(f"[Чат-{chat_id}] Не вдалося зберегти знімок екрана '{name}': сторінка вже закрита.")
            return None
        if locator:
            await locator.screenshot(path=path)
        else:
            await page.screenshot(path=path, full_page=full_page)
        logging.info(f"[Чат-{chat_id}] Знімок екрана збережено: {path}")
        return path
    except Exception as e:
        logging.error(f"[Чат-{chat_id}] Помилка при збереженні знімка екрана '{name}': {e}")
        return None

async def send_telegram_photo(bot: Bot, chat_id: int, photo_path: str, caption=""):
    if not photo_path or not os.path.exists(photo_path):
        return
    try:
        with open(photo_path, 'rb') as photo_file:
            await bot.send_photo(chat_id=chat_id, photo=photo_file, caption=caption)
        logging.info(f"[Чат-{chat_id}] Знімок екрана '{photo_path}' успішно надіслано.")
        os.remove(photo_path)
    except Exception as e:
        logging.error(f"[Чат-{chat_id}] Помилка під час надсилання фото '{photo_path}': {e}")

def load_user_data(chat_id: int) -> dict:
    user_archive_dir = os.path.join(USER_DATA_ARCHIVES_DIR, str(chat_id))
    if not os.path.isdir(user_archive_dir):
        return {}
    try:
        data_file_path = os.path.join(user_archive_dir, 'user_data.json')
        if not os.path.exists(data_file_path):
            return {}
        with open(data_file_path, 'r', encoding='utf-8') as f:
            loaded_data = json.load(f)
        key_file_name = loaded_data.get('key_file_name')
        if key_file_name:
            archived_key_path = os.path.join(user_archive_dir, key_file_name)
            if os.path.exists(archived_key_path):
                loaded_data['key_file_path'] = archived_key_path
            else:
                loaded_data.pop('key_file_path', None)
                loaded_data.pop('key_file_name', None)
        logging.info(f"Завантажено персональні дані для користувача {chat_id}")
        return loaded_data
    except Exception as e:
        logging.error(f"Помилка при завантаженні даних для {chat_id}: {e}")
        return {}

async def save_user_data(chat_id: int, user_data: dict):
    if not user_data:
        return
    user_archive_dir = os.path.join(USER_DATA_ARCHIVES_DIR, str(chat_id))
    try:
        if os.path.isdir(user_archive_dir):
            shutil.rmtree(user_archive_dir)
        os.makedirs(user_archive_dir)
        data_to_save = user_data.copy()
        key_path_to_copy = data_to_save.pop('key_file_path', None)
        data_file_path = os.path.join(user_archive_dir, 'user_data.json')
        with open(data_file_path, 'w', encoding='utf-8') as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=4)
        if key_path_to_copy and os.path.exists(key_path_to_copy):
            shutil.copy(key_path_to_copy, user_archive_dir)
        logging.info(f"Персональні дані для {chat_id} збережено.")
    except Exception as e:
        logging.error(f"Не вдалося зберегти дані для {chat_id}: {e}")

async def generate_year_selection_keyboard(base_year: int, original_month: int) -> InlineKeyboardMarkup:
    keyboard = [[InlineKeyboardButton("Виберіть рік", callback_data="calendar_ignore")]]
    start_year = base_year - 6
    year_buttons_row = []
    for year in range(start_year, start_year + 12):
        year_buttons_row.append(InlineKeyboardButton(str(year), callback_data=f"calendar_nav_{year}_{original_month}"))
        if len(year_buttons_row) == 4:
            keyboard.append(year_buttons_row)
            year_buttons_row = []
    if year_buttons_row:
        keyboard.append(year_buttons_row)
    nav_row = [
        InlineKeyboardButton("<<", callback_data=f"calendar_year_nav_{base_year - 12}_{original_month}"),
        InlineKeyboardButton("Назад", callback_data=f"calendar_nav_{base_year}_{original_month}"),
        InlineKeyboardButton(">>", callback_data=f"calendar_year_nav_{base_year + 12}_{original_month}")
    ]
    keyboard.append(nav_row)
    return InlineKeyboardMarkup(keyboard)

async def generate_month_selection_keyboard(year: int, original_month: int) -> InlineKeyboardMarkup:
    month_names = ["Січ", "Лют", "Бер", "Кві", "Тра", "Чер", "Лип", "Сер", "Вер", "Жов", "Лис", "Гру"]
    keyboard = [[InlineKeyboardButton(f"Виберіть місяць для {year} року", callback_data="calendar_ignore")]]
    month_buttons_row = []
    for i, month_name in enumerate(month_names):
        month_number = i + 1
        month_buttons_row.append(InlineKeyboardButton(month_name, callback_data=f"calendar_nav_{year}_{month_number}"))
        if len(month_buttons_row) == 4:
            keyboard.append(month_buttons_row)
            month_buttons_row = []
    keyboard.append([InlineKeyboardButton("Назад", callback_data=f"calendar_nav_{year}_{original_month}")])
    return InlineKeyboardMarkup(keyboard)

async def generate_calendar_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    month_names_uk = ["", "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень", "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"]
    cal = calendar.monthcalendar(year, month)
    keyboard = []
    header_buttons = [
        InlineKeyboardButton(f"{month_names_uk[month]}", callback_data=f"calendar_show_months_{year}_{month}"),
        InlineKeyboardButton(f"{year}", callback_data=f"calendar_show_years_{year}_{month}")
    ]
    keyboard.append(header_buttons)
    keyboard.append([InlineKeyboardButton(day, callback_data="calendar_ignore") for day in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]])
    for week in cal:
        row = [InlineKeyboardButton(str(day) if day != 0 else " ", callback_data=f"calendar_select_{year}_{month}_{day}") for day in week]
        keyboard.append(row)
    prev_month, prev_year = (month - 1, year) if month > 1 else (12, year - 1)
    next_month, next_year = (month + 1, year) if month < 12 else (1, year + 1)
    nav_buttons = [
        InlineKeyboardButton("<", callback_data=f"calendar_nav_{prev_year}_{prev_month}"),
        InlineKeyboardButton("Сьогодні", callback_data=f"calendar_nav_{datetime.now().year}_{datetime.now().month}"),
        InlineKeyboardButton(">", callback_data=f"calendar_nav_{next_year}_{next_month}")
    ]
    keyboard.append(nav_buttons)
    return InlineKeyboardMarkup(keyboard)

# --- AUTOMATION BLOCK ---

async def check_session_validity(page):
    """Checks if the e-consul session is active."""
    try:
        if "id.e-consul.gov.ua" in page.url:
            logging.warning("On login page, session is invalid.")
            return False
            
        session_ended_locator = page.locator('text="Сесія закінчилася"')
        if await session_ended_locator.is_visible():
            logging.warning("Виявлено повідомлення 'Сесія закінчилася'. Сесія недійсна.")
            return False

        logged_in_element_selector = page.get_by_role("link", name="Запис на візит")
        await expect(logged_in_element_selector).to_be_visible(timeout=10000) # Збільшено таймаут
        logging.info("Активний елемент навігації знайдено. Сесія дійсна.")
        return True
    except PlaywrightTimeoutError:
        logging.warning("Авторизований елемент не знайдено. Сесія неактивна.")
        return False
    except Exception as e:
        logging.error(f"Помилка під час перевірки дійсності сесії: {e}")
        return False

async def book_available_slot(page, bot, chat_id, log_func):
    """Finds and books the first available slot."""
    log_func("Початок автоматичного бронювання слоту.")
    try:
        month_container_locator = page.locator('div[class*="scrollbar-container"]')
        await month_container_locator.wait_for(state='visible', timeout=20000)
        month_tabs_locator = month_container_locator.get_by_text(re.compile(r"^\S+ \d{4}$"))
        month_tabs_count = await month_tabs_locator.count()
        if month_tabs_count == 0:
             raise Exception("Не вдалося знайти вкладки з місяцями.")
        log_func(f"Успішно знайдено вкладки місяців: {month_tabs_count}")
        for i in range(month_tabs_count):
            current_month_tab = page.locator('div[class*="scrollbar-container"]').get_by_text(re.compile(r"^\S+ \d{4}$")).nth(i)
            month_name = await current_month_tab.text_content()
            log_func(f"Перевіряю місяць: {month_name}")
            await current_month_tab.click(timeout=15000)
            await page.wait_for_timeout(random.randint(1500, 2500))
            await page.wait_for_load_state('networkidle', timeout=60000)
            base_locator = page.locator('div:has-text("СЛОТІВ")')
            available_week_locator = base_locator.filter(has_not_text="0 ВІЛЬНИХ")
            if await available_week_locator.count() == 0:
                continue
            log_func(f"!!! Знайдено тиждень з вільними слотами в місяці '{month_name}'!!!")
            week_block = available_week_locator.first
            path = await save_screenshot(page, f"week_with_slots_{month_name}", chat_id)
            await send_telegram_photo(bot, chat_id, path, f"Знайдено тиждень зі слотами в {month_name}.")
            await week_block.click(timeout=15000)
            await page.wait_for_load_state('networkidle', timeout=30000)
            await page.keyboard.press("End")
            await page.wait_for_timeout(1000)
            all_time_slots = await page.locator('div[role="button"][aria-label]').all()
            first_available_slot = None
            for slot in all_time_slots:
                aria_label = await slot.get_attribute("aria-label") or ""
                if "цей слот вже заброньовано" not in aria_label:
                    first_available_slot = slot
                    break
            if first_available_slot is None:
                log_func(f"У тижні '{month_name}' не знайдено вільних слотів після перевірки. Повертаюся.")
                await page.get_by_role("button", name="Назад").click(timeout=10000)
                await page.wait_for_load_state('networkidle', timeout=15000)
                continue
            slot_time_text = await first_available_slot.text_content()
            log_func(f"Знайдено вільний слот: {slot_time_text}. Вибираю...")
            await first_available_slot.click(timeout=15000)
            confirm_button_selector = 'button#finish-button'
            await page.locator(confirm_button_selector).wait_for(state='visible', timeout=15000)
            log_func("Натискаю кнопку 'Підтвердити'...")
            path = await save_screenshot(page, "final_confirmation", chat_id)
            await send_telegram_photo(bot, chat_id, path, "Екран перед фінальним підтвердженням.")
            await page.locator(confirm_button_selector).click()
            success_locator = page.locator('text="Ваш візит успішно заброньовано"')
            await expect(success_locator).to_be_visible(timeout=60000)
            final_message = "!!! УСПІХ: Візит успішно заброньовано! Перевірте пошту та сторінку 'Мої візити'."
            log_func(final_message)
            await bot.send_message(chat_id, f"🎉 **УСПІХ!** 🎉\n{final_message}")
            path = await save_screenshot(page, "booking_confirmed", chat_id)
            await send_telegram_photo(bot, chat_id, path, "Візит успішно заброньовано!")
            return "Успішне автоматичне бронювання."
        log_func("Пошук завершено. Немає вільних слотів для бронювання.")
        return "Не вдалося забронювати слот."
    except Exception as e:
        error_message = f"Критична помилка під час автоматичного бронювання: {e}"
        log_func(error_message)
        await bot.send_message(chat_id, f"❗️ Помилка під час бронювання: {e}")
        path = await save_screenshot(page, "booking_error", chat_id)
        await send_telegram_photo(bot, chat_id, path, "Помилка на етапі бронювання.")
        raise SessionResetException(f"Помилка під час бронювання: {e}")

async def run_booking_session(bot: Bot, chat_id: int, user_data: dict, worker_id: int, headless_mode: bool):
    """Main worker function to perform the booking using a persistent profile."""
    p = context = page = None
    log_func = lambda msg: logging.info(f"[Воркер-{worker_id}][Чат-{chat_id}] {msg}")
    user_data_dir = os.path.join(BASE_PROFILES_DIR, f"profile_{chat_id}")

    try:
        log_func("="*20 + " ПОЧАТОК НОВОЇ СЕСІЇ " + "="*20)
        p = await async_playwright().start()
        
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=headless_mode,
            channel="chrome",
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            locale="uk-UA",
            timezone_id="Europe/Kiev",
            viewport=None,
            # Таймаут для запуску браузера
            timeout=90000 
        )
        
        page = context.pages[0] if context.pages else await context.new_page()

        await page.evaluate("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        log_func("Маскування navigator.webdriver застосовано.")

        await page.goto("https://e-consul.gov.ua/", timeout=120000, wait_until="domcontentloaded")
        log_func(f"Сторінку завантажено. URL: {page.url}")

        is_logged_in = await check_session_validity(page)
        if not is_logged_in:
            log_func("Сесія неактивна. Починаю процес авторизації...")
            await bot.send_message(chat_id, "Сесія неактивна. Починаю авторизацію...")
            
            if "id.e-consul.gov.ua" not in page.url:
                 await page.goto("https://id.e-consul.gov.ua/", timeout=60000)
            
            auth_method = user_data['auth_method']
            if auth_method == 'diia':
                await page.get_by_role("button", name="Дія.Підпис").click()
                await page.wait_for_load_state('networkidle')
                await page.wait_for_timeout(3000) 
                qr_element = page.locator("canvas, div.qr-code").first
                await qr_element.wait_for(state='visible', timeout=60000)
                qr_path = await save_screenshot(page, "diia_qr_code", chat_id, locator=qr_element)
                await send_telegram_photo(bot, chat_id, qr_path, "Відскануйте QR-код у застосунку 'Дія' для авторизації.")
                await bot.send_message(chat_id, "Очікую підтвердження в 'Дія'. У вас є 3 хвилини.")
                await page.wait_for_url("**/e-consul.gov.ua/**", timeout=180000)
            elif auth_method == 'key':
                await page.get_by_role("button", name="Особистий ключ").click()
                await page.wait_for_load_state('networkidle')
                async with page.expect_file_chooser() as fc_info:
                    await page.locator('text="Оберіть ключ на своєму носієві"').click()
                await (await fc_info.value).set_files(user_data['key_file_path'])
                await page.locator('#id-app-login-sign-form-file-key-server').click()
                await page.locator('li[data-value="31"]').click()
                await page.locator('input#id-app-login-sign-form-file-key-password').fill(user_data['key_password'])
                async with page.expect_navigation(url="**/e-consul.gov.ua/**"):
                    await page.locator('button#id-app-login-sign-form-file-key-sign-button').click()
            elif auth_method == 'idgov':
                await page.locator('button').nth(2).click()
                await page.get_by_text("Bank ID НБУ").click()
                await page.locator('#selBankConnect-button').click()
                await page.locator('#ui-id-4').click()
                async with page.expect_navigation(url="**/api.monobank.ua/**"):
                    await page.locator('#btnBankIDChoose').click()
                await page.wait_for_timeout(3000)
                qr_element = page.locator("#qr-overlay, div.qr-code, canvas").first
                await qr_element.wait_for(state='visible', timeout=60000)
                qr_path = await save_screenshot(page, "bankid_qr_code", chat_id, locator=qr_element)
                await send_telegram_photo(bot, chat_id, qr_path, "Відскануйте QR-код у додатку Monobank.")
                await page.wait_for_url("**/id.gov.ua/bankid-auth-callback**", timeout=180000)
                async with page.expect_navigation(url="**/e-consul.gov.ua/**"):
                    await page.locator('#btnAcceptUserDataAgreement').click()
            
            await page.wait_for_load_state('networkidle')
            await bot.send_message(chat_id, "✅ Авторизація успішна. Профіль сесії збережено.")
            log_func(f"Авторизація успішна. URL: {page.url}")
        else:
            await bot.send_message(chat_id, "Сесія активна, використовую збережений профіль.")
            log_func("Сесія активна, пропускаю авторизацію.")

        await bot.send_message(chat_id, "Переходжу до створення запису та заповнення даних...")
        await page.get_by_role("link", name="Запис на візит").click()
        await page.get_by_role("link", name="Записатись на візит").click()
        await page.wait_for_url("**/applicantInfo**", timeout=60000)

        dob = datetime.strptime(user_data['date_of_birth'], "%d.%m.%Y")
        await page.locator('input[aria-label="день"]').fill(str(dob.day))
        month_names_uk = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]
        await page.locator('div[aria-label="місяць"]').click()
        await page.get_by_role("option", name=month_names_uk[dob.month - 1]).click()
        await page.locator('input[aria-label="рік"]').fill(str(dob.year))
        gender_map = {'male': "Чоловіча", 'female': "Жіноча"}
        await page.get_by_label(gender_map[user_data['gender']]).check()
        await page.get_by_role("button", name="Далі").click()
        await page.wait_for_url("**/placeOfVisitInfo**", timeout=60000)
        
        await page.locator('input[id="country"]').fill(user_data['country'])
        await page.locator(f"li:has-text(\"{user_data['country']}\")").click()
        await page.wait_for_timeout(2000)
        await page.locator('input[id="consularInstitution"]').fill(user_data['institution'])
        await page.locator(f"li:has-text(\"{user_data['institution']}\")").click()
        await page.get_by_role("button", name="Далі").click()
        await page.wait_for_url("**/consularServiceInfo**", timeout=60000)

        service_input = page.locator('input[id="service"]')
        await service_input.fill(user_data['service_type'], timeout=10000)
        await page.wait_for_timeout(1000)
        await page.get_by_role("option", name=user_data['service_type'], exact=True).click(timeout=10000)
        
        recipient_map = {'self': "Для себе", 'child': "Для своєї дитини, підопічного"}
        await page.get_by_label(recipient_map[user_data['service_recipient']]).check()
        if user_data['service_recipient'] == 'child':
            await page.locator('input[aria-label="Прізвище"]').fill(user_data['child_surname'])
            await page.locator('input[aria-label=" Ім\'я"]').fill(user_data['child_firstname'])
            await page.locator('input[aria-label="По батькові"]').fill(user_data['child_middlename'])
        await page.get_by_role("button", name="Далі").click()
        log_func("Всі дані успішно введено.")
        await bot.send_message(chat_id, "✅ Всі дані успішно введено.")
        await page.wait_for_url("**/dateAndTimeVisitInfo**", timeout=60000)
        
        await bot.send_message(chat_id, "Починаю пошук вільних слотів. Це може зайняти багато часу...")
        log_func("Починаю цикл пошуку слотів.")
        
        max_retries = 99999
        for i in range(max_retries):
            log_func(f"Спроба пошуку слотів #{i + 1}")
            await expect(page.locator('text="Відбувається пошук активних слотів..."')).to_be_hidden(timeout=120000)
            await page.wait_for_timeout(5000)

            if await page.locator('div[class*="scrollbar-container"]').is_visible():
                log_func("!!! КАЛЕНДАР ЗНАЙДЕНО! Слоти доступні !!!")
                await bot.send_message(chat_id, "🔥 **ЗНАЙДЕНО СЛОТИ!** 🔥\nПочинаю бронювання...")
                result = await book_available_slot(page, bot, chat_id, log_func)
                if "Успішне" in result:
                    return
                else:
                    raise SessionResetException("Бронювання не вдалося, перезапускаю сесію для нової спроби.")
            else:
                log_func("Слотів немає. Перезавантажую сторінку...")
                await page.reload(wait_until='networkidle')
                await asyncio.sleep(random.randint(10, 20))

    except SessionResetException as e:
        log_func(f"Штатний перезапуск сесії: {e}")
        raise
    except Exception as e:
        error_message = f"Сталася критична помилка в run_booking_session: {e}"
        log_func(error_message)
        full_traceback = traceback.format_exc()
        logging.error("\nПОВНИЙ ТРЕЙСБЕК ПОМИЛКИ:\n" + full_traceback)
        await bot.send_message(chat_id, f"❗️ **Сталася помилка у воркері.**\n\n`{str(e)}`\n\nЗавдання буде перезапущено.")
        if page and not page.is_closed():
            await save_screenshot(page, "error_screenshot", chat_id)
        raise SessionResetException(f"Критична помилка: {e}")
    finally:
        if context: await context.close()
        if p: await p.stop()
        log_func("="*20 + " КІНЕЦЬ СЕСІЇ " + "="*20 + "\n")

# --- Worker and Telegram Handlers ---

async def worker(worker_id: int, app: Application, headless_mode: bool):
    logging.info(f"[Воркер-{worker_id}] запущений.")
    while True:
        try:
            chat_id = await task_queue.get()
            logging.info(f"[Воркер-{worker_id}] Взяв у роботу користувача {chat_id}.")
            user_data = user_sessions.get(chat_id)
            if not user_data or user_data.get('status') != 'ready':
                logging.warning(f"[Воркер-{worker_id}] Дані для {chat_id} не знайдено, завдання пропущено.")
                task_queue.task_done()
                continue
            
            max_restarts = 5
            for attempt in range(max_restarts):
                try:
                    await run_booking_session(app.bot, chat_id, user_data, worker_id, headless_mode)
                    logging.info(f"[Воркер-{worker_id}] Завдання для {chat_id} успішно виконано.")
                    break 
                except SessionResetException as e:
                    logging.warning(f"[Воркер-{worker_id}] Перезапуск сесії для {chat_id} (спроба {attempt + 1}/{max_restarts}). Причина: {e}")
                    if attempt + 1 >= max_restarts:
                        logging.error(f"Досягнуто ліміту перезапусків для {chat_id}. Завдання зупинено.")
                        await app.bot.send_message(chat_id, "Досягнуто ліміту автоматичних перезапусків. Завдання зупинено.")
                        break
                    await asyncio.sleep(15)

            logging.info(f"[Воркер-{worker_id}] Завершив роботу з користувачем {chat_id}.")
            if chat_id in user_sessions:
                del user_sessions[chat_id]
            task_queue.task_done()
        except Exception as e:
            logging.critical(f"Критична помилка у воркері {worker_id}: {e}")

async def update_lists_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    owner_id = os.getenv('OWNER_TELEGRAM_ID')

    if not owner_id:
        await update.message.reply_text("❗️ Команду не налаштовано: ID адміністратора не вказано.")
        logging.warning(f"Користувач {user_id} намагався виконати /update_lists, але OWNER_TELEGRAM_ID не встановлено.")
        return ConversationHandler.END

    if user_id != owner_id:
        await update.message.reply_text("🚫 У вас немає прав для виконання цієї команди.")
        logging.warning(f"Користувач {user_id} (не адмін) намагався виконати /update_lists.")
        return ConversationHandler.END

    logging.info(f"Адміністратор {get_user_info(update)} виконав команду /update_lists")
    await update.message.reply_text(
        "**Оновлення довідників (лише для адміністратора)**\n\n"
        "Цей процес запустить браузер. Вам потрібно буде вручну авторизуватися на сайті e-consul.\n"
        "Після успішного входу, не закривайте браузер. Скрипт автоматично збере дані.\n\n"
        "Натисніть 'Почати', щоб продовжити.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Почати", callback_data="start_scraping")]])
    )
    return UPDATING_LISTS

async def start_scraping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Збирає списки країн, установ та послуг, з періодичним перезавантаженням сторінки."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    bot = context.bot
    
    user_data_dir = os.path.join(BASE_PROFILES_DIR, f"profile_{chat_id}")
    await query.edit_message_text(f"Запускаю браузер з постійним профілем...")

    p = browser_context = page = None
    try:
        p = await async_playwright().start() 
        browser_context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            channel="chrome",
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"]
        )
        page = browser_context.pages[0] if browser_context.pages else await browser_context.new_page()
        
        await page.evaluate("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        logging.info("Маскування navigator.webdriver застосовано.")
        
        await page.goto("https://e-consul.gov.ua/", timeout=60000, wait_until="domcontentloaded")
        await bot.send_message(chat_id, 
            "Браузер запущено. Будь ласка, увійдіть до свого облікового запису на сайті e-consul.\n"
            "У вас є 5 хвилин. Після успішного входу, поверніться сюди.")

        await page.wait_for_url("**/messages**", timeout=300000)
        await bot.send_message(chat_id, "✅ Авторизація пройдена. Починаю збір даних...")
        
        async def navigate_to_country_form():
            """Надійна допоміжна функція для навігації до форми вибору країни."""
            try:
                if "placeOfVisitInfo" in page.url:
                    logging.info("Вже знаходимось на сторінці 'Місце візиту'. Пропускаю навігацію.")
                    await page.locator('input[id="country"]').wait_for(state='visible', timeout=15000)
                    return

                logging.info("Перехід до форми вибору країни...")
                visit_link_locator = page.get_by_role("link", name="Запис на візит")
                await visit_link_locator.wait_for(state="visible", timeout=30000)
                await visit_link_locator.click()

                book_visit_locator = page.get_by_role("link", name="Записатись на візит")
                await book_visit_locator.wait_for(state="visible", timeout=30000)
                await book_visit_locator.click()

                await page.wait_for_url("**/applicantInfo**", timeout=60000)
                await page.locator('input[aria-label="день"]').fill("1")
                await page.locator('div[aria-label="місяць"]').click()
                await page.get_by_role("option", name="січня").click()
                await page.locator('input[aria-label="рік"]').fill("2000")
                await page.get_by_label("Жіноча").check()
                await page.get_by_role("button", name="Далі").click()
                
                await page.wait_for_url("**/placeOfVisitInfo**", timeout=60000)
                logging.info("Успішно перейшли до форми вибору країни.")
            except PlaywrightTimeoutError as e:
                logging.error(f"Таймаут під час навігації: {str(e).splitlines()[0]}")
                
                try:
                    await bot.send_message(chat_id, "❗️ **Помилка:** Не вдалося знайти ключовий елемент навігації. Спробую зібрати діагностичну інформацію...")
                    
                    html_content = await page.content()
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    debug_dir = os.path.join(BASE_SCREENSHOTS_DIR, str(chat_id))
                    os.makedirs(debug_dir, exist_ok=True)
                    html_path = os.path.join(debug_dir, f"debug_page_{timestamp}.html")
                    with open(html_path, 'w', encoding='utf-8') as f:
                        f.write(html_content)
                    
                    with open(html_path, 'rb') as html_file:
                        await bot.send_document(chat_id=chat_id, document=html_file, caption="HTML-код сторінки в момент помилки.")
                    logging.info(f"Надіслано файл з HTML-кодом: {html_path}")
                    os.remove(html_path)

                    path = await save_screenshot(page, "navigation_failed", chat_id)
                    await send_telegram_photo(bot, chat_id, path, caption="Знімок екрана в момент помилки.")

                except Exception as report_error:
                    logging.error(f"!!! Не вдалося надіслати діагностичний звіт: {report_error}")
                    try:
                        await bot.send_message(chat_id, "❗️ **Помилка:** Не вдалося знайти ключовий елемент. Спроба надіслати діагностичні файли також не вдалася, ймовірно, через проблеми з мережею.")
                    except Exception as final_report_error:
                        logging.error(f"!!! Не вдалося надіслати навіть фінальне повідомлення про помилку: {final_report_error}")

                raise SessionResetException("Помилка навігації, потрібно перезапустити.")

        await navigate_to_country_form()
        
        await bot.send_message(chat_id, "Етап 1: Збираю повний список країн...")
        
        country_input_selector = 'input[id="country"]'
        institution_input_selector = 'input[id="consularInstitution"]'
        options_selector = 'div[class*="MuiAutocomplete-popper"] li.MuiAutocomplete-option'

        await page.locator(country_input_selector).click()
        await page.locator(options_selector).first.wait_for(state='visible', timeout=15000)
        
        found_countries = set()
        no_new_countries_streak = 0
        for i in range(200):
            current_options = await page.locator(options_selector).all()
            initial_count = len(found_countries)
            for option in current_options:
                text = await option.text_content()
                if text: found_countries.add(text.strip())
            if len(found_countries) > initial_count: no_new_countries_streak = 0
            else: no_new_countries_streak += 1
            if no_new_countries_streak >= 15: break
            await page.locator(country_input_selector).press("ArrowDown")
            await page.wait_for_timeout(50)
        country_list = sorted(list(found_countries))
        await page.locator('body').click()
        await page.wait_for_timeout(500)

        logging.info("Список країн зібрано. Перезавантажую сторінку для стабілізації перед основною обробкою.")
        await bot.send_message(chat_id, f"✅ Список з {len(country_list)} країн отримано. Етап 2: Оновлюю сторінку для стабільної роботи...")
        
        await page.reload(wait_until="domcontentloaded", timeout=60000)
        
        await navigate_to_country_form()

        scraped_data = {'countries': country_list, 'institutions': {}}
        await bot.send_message(chat_id, f"Етап 3: Починаю збір консульств для кожної країни...")
        
        for i, country in enumerate(country_list):
            try:
                if (i > 0) and (i % 5 == 0):
                    logging.info(f"--- Періодичне перезавантаження сторінки після обробки {i} країн ---")
                    await bot.send_message(chat_id, f"⚙️ Оновлюю сесію для стабільності (оброблено {i}/{len(country_list)})...")
                    
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                    
                    await navigate_to_country_form()
                    logging.info("--- Сторінку перезавантажено, продовжую збір ---")

                country_input = page.locator(country_input_selector)
                await country_input.wait_for(state='visible', timeout=15000)
                await country_input.clear()
                await page.locator(institution_input_selector).clear()
                await page.wait_for_timeout(500)

                await country_input.fill(country)
                await page.get_by_role("option", name=country, exact=True).click(timeout=10000)
                
                await page.locator(institution_input_selector).click()
                await page.locator(options_selector).first.wait_for(state='visible', timeout=10000)
                inst_locators = page.locator(options_selector)
                institutions = [await loc.text_content() for loc in await inst_locators.all()]
                scraped_data['institutions'][country] = sorted(institutions)
                logging.info(f"({i + 1}/{len(country_list)}) Зібрано установи для: {country}")
                await page.locator('body').click()
                await page.wait_for_timeout(random.randint(1000, 1500))

                if (i + 1) % 15 == 0 and (i+1) % 5 != 0:
                    await bot.send_message(chat_id, f"Оброблено {i + 1}/{len(country_list)} країн...")
            
            except PlaywrightTimeoutError as e:
                error_msg_text = str(e)
                logging.error(f"Не вдалося обробити країну '{country}' через таймаут: {error_msg_text.splitlines()[0]}. Пропускаю і спробую перезавантажити сторінку.")
                try:
                    await bot.send_message(chat_id, f"❗️ Схоже, сторінка 'зависла' на країні '{country}'. Перезавантажую, щоб продовжити...")
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                    await navigate_to_country_form()
                except Exception as recovery_err:
                    logging.error(f"Критична помилка під час спроби відновлення: {recovery_err}")
                    await bot.send_message(chat_id, "❗️ Не вдалося відновити роботу після збою. Зупиняю процес.")
                    raise SessionResetException("Невдала спроба відновлення.")
                continue

            except Exception as e:
                error_msg_text = str(e)
                logging.error(f"Не вдалося обробити країну '{country}': {error_msg_text.splitlines()[0]}. Пропускаю.")
                if "browser has been closed" in error_msg_text:
                    logging.critical("Браузер було закрито. Припиняю збір даних.")
                    await bot.send_message(chat_id, "❗️ **Критична помилка**: Браузер несподівано закрився. Процес збору зупинено.")
                    break
        
        if not browser_context.is_closed():
            await bot.send_message(chat_id, "Збір установ завершено. Переходжу до збору списку послуг...")
            first_country_with_inst = next((c for c in country_list if scraped_data['institutions'].get(c)), None)
            if not first_country_with_inst:
                raise Exception("Не вдалося знайти жодної країни з установами для продовження.")
            first_institution = scraped_data['institutions'][first_country_with_inst][0]
            
            await page.locator(country_input_selector).fill(first_country_with_inst)
            await page.get_by_role("option", name=first_country_with_inst, exact=True).click()
            await page.wait_for_timeout(500)
            await page.locator(institution_input_selector).fill(first_institution)
            await page.get_by_role("option", name=first_institution, exact=True).click()
            await page.get_by_role("button", name="Далі").click()
            await page.wait_for_url("**/consularServiceInfo**", timeout=60000)
            
            await bot.send_message(chat_id, "На сторінці вибору послуг. Починаю збір з прокруткою...")
            service_input_selector = 'input[id="service"]'
            await page.locator(service_input_selector).click(timeout=10000)
            await page.get_by_role("option").first.wait_for(state='visible', timeout=15000)
            
            found_services = set()
            no_new_services_streak = 0
            for i in range(200):
                current_options = await page.locator(options_selector).all()
                initial_count = len(found_services)
                for option in current_options:
                    text = await option.text_content()
                    if text: found_services.add(text.strip())
                if len(found_services) > initial_count: no_new_services_streak = 0
                else: no_new_services_streak += 1
                if no_new_services_streak >= 15: break
                await page.locator(service_input_selector).press("ArrowDown")
                await page.wait_for_timeout(50)
                
            all_available_services = sorted(list(found_services))
            scraped_data['services'] = all_available_services
            logging.info(f"Успішно зібрано {len(all_available_services)} унікальних послуг.")

            with open(DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(scraped_data, f, ensure_ascii=False, indent=4)

            await bot.send_message(chat_id, f"🎉 **Готово!**\nДовідники успішно оновлено.\n"
                                            f"Знайдено країн з установами: {len(scraped_data['institutions'])}/{len(country_list)}\n"
                                            f"Знайдено консульських послуг: {len(all_available_services)}")
    except PlaywrightTimeoutError as e:
        error_text = str(e).splitlines()[0]
        logging.error(f"Стався таймаут на одному з кроків: {error_text}")
        await bot.send_message(chat_id, f"❗️ **Помилка таймауту:**\n`{error_text}`")
        if page and not page.is_closed():
            path = await save_screenshot(page, "timeout_error", chat_id)
            await send_telegram_photo(bot, chat_id, path, caption="Знімок екрана в момент помилки")
    except Exception as e:
        logging.error(f"Сталася критична помилка в start_scraping: {e}", exc_info=True)
        await bot.send_message(chat_id, f"❗️ Сталася критична помилка: {str(e).splitlines()[0]}")
    finally:
        if browser_context: await browser_context.close()
        if p: await p.stop()
    return ConversationHandler.END

# --- Telegram Handlers ---
(
    MAIN_MENU, SELECTING_AUTH, AWAITING_KEY_FILE, AWAITING_PASSWORD,
    AWAITING_DOB_GENDER, AWAITING_DOB_CALENDAR,
    AWAITING_COUNTRY, AWAITING_INSTITUTION,
    AWAITING_SERVICE, AWAITING_RECIPIENT, AWAITING_CHILD_DATA
) = range(11)

UPDATING_LISTS = 11

def get_user_info(update: Update) -> str:
    user = update.effective_user
    return f"user_id: {user.id}, name: '{user.first_name}', username: @{user.username}"

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.info(f"Користувач {get_user_info(update)} виконав команду /start")
    await update.message.reply_text("👋 **Привіт!**\n\nЯ бот для запису на консульські послуги. Використовуйте /menu або /keyboard для початку.")

async def start_new_booking_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """General function to start a new setup."""
    chat_id = update.effective_chat.id
    if chat_id in list(task_queue._queue) or (user_sessions.get(chat_id) and user_sessions[chat_id].get('status') == 'ready'):
        await context.bot.send_message(chat_id, "❗️ Ваше завдання вже в черзі. Для скасування використовуйте /cancel.")
        return ConversationHandler.END
    
    user_sessions[chat_id] = {'chat_id': chat_id}
    keyboard = [
        [InlineKeyboardButton("Дія.Підпис", callback_data='diia')],
        [InlineKeyboardButton("Особистий ключ (файл)", callback_data='key')],
        [InlineKeyboardButton("ID.GOV.UA (BankID)", callback_data='idgov')],
    ]
    await context.bot.send_message(
        chat_id,
        "**Крок 1/5: Метод входу**\n\nВиберіть метод автентифікації:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECTING_AUTH

async def book_new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logging.info(f"Користувач {get_user_info(update)} виконав команду /book_new")
    return await start_new_booking_flow(update, context)

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Displays the main interactive menu."""
    message_source = update.callback_query.message if update.callback_query else update.message
    keyboard = [
        [InlineKeyboardButton("📝 Почати новий запис", callback_data="main_book_new")],
        [InlineKeyboardButton("📊 Дізнатися статус", callback_data="main_status")]
    ]
    await message_source.reply_text(
        "**Головне меню**",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return MAIN_MENU

async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles main menu button presses."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "main_book_new":
        await query.message.delete()
        return await start_new_booking_flow(query, context)
    elif query.data == "main_status":
        await query.message.delete()
        await status_command(query, context)
        return ConversationHandler.END

async def book_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logging.info(f"Користувач {get_user_info(update)} виконав команду /book")
    chat_id = update.effective_chat.id
    if chat_id in list(task_queue._queue) or (user_sessions.get(chat_id) and user_sessions[chat_id].get('status') == 'ready'):
        await update.message.reply_text("❗️ Ваше завдання вже в черзі. Для скасування використовуйте /cancel.")
        return ConversationHandler.END
    persisted_data = load_user_data(chat_id)
    if not persisted_data:
        await update.message.reply_text("Збережені дані не знайдено. Починаю нове налаштування...")
        return await start_new_booking_flow(update, context)
    
    user_sessions[chat_id] = persisted_data
    user_sessions[chat_id]['status'] = 'ready'
    await task_queue.put(chat_id)
    logging.info(f"Користувач {chat_id} доданий до черги зі збереженими даними.")
    await update.message.reply_text("✅ **Використовую збережені дані.**\nЗавдання додано до черги.")
    return ConversationHandler.END

async def select_auth_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    auth_method = query.data
    user_sessions[chat_id]['auth_method'] = auth_method
    await query.edit_message_text(text=f"Вибраний метод: **{auth_method.upper()}**.")
    if auth_method == 'key':
        await context.bot.send_message(chat_id, "Прикріпіть ваш файл ключа (`.dat`, `.pfx`, тощо).")
        return AWAITING_KEY_FILE
    await context.bot.send_message(chat_id, "**Крок 2/5: Дані заявника**\n\nОберіть вашу стать:",
                                   reply_markup=InlineKeyboardMarkup([
                                       [InlineKeyboardButton("Жіноча", callback_data='gender_female'),
                                        InlineKeyboardButton("Чоловіча", callback_data='gender_male')]
                                   ]))
    return AWAITING_DOB_GENDER

async def receive_key_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    document = update.message.document
    keys_dir = os.path.join(BASE_KEYS_DIR, str(chat_id))
    os.makedirs(keys_dir, exist_ok=True)
    file_path = os.path.join(keys_dir, document.file_name)
    file = await document.get_file()
    await file.download_to_drive(file_path)
    user_sessions[chat_id]['key_file_path'] = file_path
    user_sessions[chat_id]['key_file_name'] = document.file_name
    await update.message.reply_text("✅ Файл отримано. Тепер введіть пароль до цього ключа.")
    return AWAITING_PASSWORD

async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    user_sessions[chat_id]['key_password'] = update.message.text
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        await update.message.reply_text("Пароль отримано та видалено.")
    except: pass
    await context.bot.send_message(chat_id, "**Крок 2/5: Дані заявника**\n\nОберіть вашу стать:",
                                   reply_markup=InlineKeyboardMarkup([
                                       [InlineKeyboardButton("Жіноча", callback_data='gender_female'),
                                        InlineKeyboardButton("Чоловіча", callback_data='gender_male')]
                                   ]))
    return AWAITING_DOB_GENDER

async def select_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    user_sessions[chat_id]['gender'] = query.data.split('_')[1]
    now = datetime.now()
    await query.edit_message_text("Стать обрано. Тепер виберіть вашу дату народження:", 
                                  reply_markup=await generate_calendar_keyboard(now.year, now.month))
    return AWAITING_DOB_CALENDAR

async def calendar_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    action, *params = query.data.split('_')
    if action != "calendar":
        return AWAITING_DOB_CALENDAR
    command = params[0]
    if command == "nav":
        year, month = map(int, params[1:])
        await query.edit_message_reply_markup(reply_markup=await generate_calendar_keyboard(year, month))
    elif command == "show" and params[1] == "months":
        year, month = map(int, params[2:])
        await query.edit_message_reply_markup(reply_markup=await generate_month_selection_keyboard(year, month))
    elif command == "show" and params[1] == "years":
        year, month = map(int, params[2:])
        await query.edit_message_reply_markup(reply_markup=await generate_year_selection_keyboard(year, month))
    elif command == "year" and params[1] == "nav":
        year, month = map(int, params[2:])
        await query.edit_message_reply_markup(reply_markup=await generate_year_selection_keyboard(year, month))
    elif command == "select":
        year, month, day = map(int, params[1:])
        if day == 0: return AWAITING_DOB_CALENDAR
        selected_date_str = f"{day:02d}.{month:02d}.{year}"
        user_sessions[chat_id]['date_of_birth'] = selected_date_str
        await query.edit_message_text(f"Обрана дата: {selected_date_str}")
        
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                all_countries = data['countries']
        except (FileNotFoundError, KeyError):
            await context.bot.send_message(chat_id, f"❗️ Файл з даними `{DATA_FILE}` не знайдено. Запустіть /update_lists.")
            return ConversationHandler.END

        main_countries_def = ['Ірландія', 'Польща', 'Німеччина', 'США', 'Чехія', 'Іспанія', 'Італія', 'Канада']
        main_countries = [c for c in main_countries_def if c in all_countries]
        other_countries = sorted([c for c in all_countries if c not in main_countries])

        context.chat_data['main_countries'] = main_countries
        context.chat_data['other_countries'] = other_countries
        
        keyboard = [[InlineKeyboardButton(country, callback_data=f"country_{country}")] for country in main_countries]
        if other_countries:
            keyboard.append([InlineKeyboardButton("Інші країни >>", callback_data="show_other_countries")])
        
        await context.bot.send_message(chat_id, "**Крок 3/5: Місцезнаходження**\n\nВиберіть країну:", 
                                       reply_markup=InlineKeyboardMarkup(keyboard))
        return AWAITING_COUNTRY
    return AWAITING_DOB_CALENDAR

async def select_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    page_size = 8
    
    def get_paginated_menu(countries, page_num, callback_prefix, back_callback=None):
        keyboard = []
        start_index = page_num * page_size
        end_index = start_index + page_size
        for country in countries[start_index:end_index]:
             keyboard.append([InlineKeyboardButton(country, callback_data=f"country_{country}")])
        nav_row = []
        if page_num > 0:
            nav_row.append(InlineKeyboardButton("<<", callback_data=f"{callback_prefix}_prev"))
        if end_index < len(countries):
             nav_row.append(InlineKeyboardButton(">>", callback_data=f"{callback_prefix}_next"))
        if nav_row: keyboard.append(nav_row)
        if back_callback: keyboard.append([InlineKeyboardButton("<< Назад", callback_data=back_callback)])
        return InlineKeyboardMarkup(keyboard)

    if query.data == "show_other_countries":
        context.chat_data['other_countries_page'] = 0
        await query.edit_message_reply_markup(reply_markup=get_paginated_menu(context.chat_data['other_countries'], 0, "other_countries", "show_main_countries"))
        return AWAITING_COUNTRY
    
    if query.data in ["other_countries_prev", "other_countries_next"]:
        page = context.chat_data.get('other_countries_page', 0)
        page = max(0, page - 1) if query.data.endswith("_prev") else page + 1
        context.chat_data['other_countries_page'] = page
        await query.edit_message_reply_markup(reply_markup=get_paginated_menu(context.chat_data['other_countries'], page, "other_countries", "show_main_countries"))
        return AWAITING_COUNTRY

    if query.data == "show_main_countries":
        main_countries_menu_keyboard = []
        for country in context.chat_data['main_countries']:
            main_countries_menu_keyboard.append([InlineKeyboardButton(country, callback_data=f"country_{country}")])
        if context.chat_data['other_countries']:
            main_countries_menu_keyboard.append([InlineKeyboardButton("Інші країни >>", callback_data="show_other_countries")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(main_countries_menu_keyboard))
        return AWAITING_COUNTRY
        
    country_name = query.data.split('_', 1)[1]
    user_sessions[chat_id]['country'] = country_name
    
    with open(DATA_FILE, 'r', encoding='utf-8') as f: data = json.load(f)
    institutions_list = data['institutions'].get(country_name, [])

    if not institutions_list:
        await query.edit_message_text(f"Для країни {country_name} не знайдено установ.")
        return ConversationHandler.END
    
    keyboard = [[InlineKeyboardButton(text=(name[:60] + '..') if len(name) > 62 else name, callback_data=f"inst_{i}")] for i, name in enumerate(institutions_list)]
    await query.edit_message_text(f"Країна: {country_name}. Виберіть установу:", reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAITING_INSTITUTION

async def select_institution(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    
    with open(DATA_FILE, 'r', encoding='utf-8') as f: data = json.load(f)
    country = user_sessions[chat_id]['country']
    institutions_list = data['institutions'].get(country, [])
    
    inst_index = int(query.data.split('_', 1)[1])
    inst_name = institutions_list[inst_index]
    user_sessions[chat_id]['institution'] = inst_name
    
    try:
        all_available_services = data['services']
    except KeyError:
        await query.edit_message_text(f"❗️ Список послуг не знайдено у `{DATA_FILE}`. Запустіть /update_lists.")
        return ConversationHandler.END

    main_service_options_def = [
        "Оформлення закордонного паспорта",
        "Оформлення закордонного паспорта дітям 12 – 16 років",
        "Оформлення закордонного паспорта дітям до 12 років",
    ]
    main_service_options = [s for s in main_service_options_def if s in all_available_services]
    other_service_options = sorted([s for s in all_available_services if s not in main_service_options and s])
    
    context.chat_data['all_services_map'] = {f"service_{i}": text for i, text in enumerate(main_service_options + other_service_options)}
    
    keyboard = []
    for opt in main_service_options:
        cb_data = next((key for key, value in context.chat_data['all_services_map'].items() if value == opt), None)
        if cb_data:
            keyboard.append([InlineKeyboardButton(text=opt, callback_data=cb_data)])
    if other_service_options:
        keyboard.append([InlineKeyboardButton(text="Інші послуги »", callback_data="show_other_services")])
    
    await query.edit_message_text(f"Установа: {inst_name}.\n\n**Крок 4/5: Послуга**\n\nВиберіть тип послуги:",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAITING_SERVICE

async def select_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    
    all_services_map = context.chat_data.get('all_services_map', {})
    if not all_services_map:
        await query.edit_message_text("Сталася помилка, спробуйте знову /book_new")
        return ConversationHandler.END
    
    all_available_services = list(all_services_map.values())
    main_service_options_def = [
        "Оформлення закордонного паспорта",
        "Оформлення закордонного паспорта дітям 12 – 16 років",
        "Оформлення закордонного паспорта дітям до 12 років",
    ]
    main_service_options = [s for s in main_service_options_def if s in all_available_services]
    other_service_options = sorted([s for s in all_available_services if s not in main_service_options and s])

    def get_main_menu():
        keyboard = []
        for opt in main_service_options:
            cb_data = next((key for key, value in all_services_map.items() if value == opt), None)
            if cb_data: keyboard.append([InlineKeyboardButton(text=opt, callback_data=cb_data)])
        if other_service_options:
            keyboard.append([InlineKeyboardButton(text="Інші послуги »", callback_data="show_other_services")])
        return InlineKeyboardMarkup(keyboard)

    def get_other_menu():
        keyboard = []
        for opt in other_service_options:
            cb_data = next((key for key, value in all_services_map.items() if value == opt), None)
            if cb_data:
                button_text = (opt[:58] + '..') if len(opt) > 60 else opt
                keyboard.append([InlineKeyboardButton(text=button_text, callback_data=cb_data)])
        keyboard.append([InlineKeyboardButton(text="« Назад до основних", callback_data="show_main_services")])
        return InlineKeyboardMarkup(keyboard)

    callback_data = query.data
    if callback_data == "show_other_services":
        await query.edit_message_reply_markup(reply_markup=get_other_menu())
        return AWAITING_SERVICE
    
    if callback_data == "show_main_services":
        await query.edit_message_reply_markup(reply_markup=get_main_menu())
        return AWAITING_SERVICE
    
    if callback_data.startswith("service_"):
        selected_service = all_services_map.get(callback_data)
        if selected_service:
            user_sessions[chat_id]['service_type'] = selected_service
            keyboard_recipient = [
                [InlineKeyboardButton("Для себе", callback_data="recipient_self")],
                [InlineKeyboardButton("Для своєї дитини/підопічного", callback_data="recipient_child")]
            ]
            await query.edit_message_text(f"Послуга: {selected_service}.\n\n**Крок 5/5: Отримувач**\n\nДля кого послуга?",
                                          reply_markup=InlineKeyboardMarkup(keyboard_recipient))
            return AWAITING_RECIPIENT
    
    return AWAITING_SERVICE

async def select_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    recipient = query.data.split('_')[1]
    user_sessions[chat_id]['service_recipient'] = recipient
    if recipient == 'child':
        await query.edit_message_text("Введіть ПІБ дитини у форматі: Прізвище Ім'я По-батькові")
        return AWAITING_CHILD_DATA
    else:
        return await finalize_setup(query.message, chat_id, context)

async def receive_child_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    parts = update.message.text.split()
    if len(parts) >= 2:
        user_sessions[chat_id]['child_surname'] = parts[0]
        user_sessions[chat_id]['child_firstname'] = parts[1]
        user_sessions[chat_id]['child_middlename'] = " ".join(parts[2:])
        return await finalize_setup(update.message, chat_id, context)
    else:
        await update.message.reply_text("Невірний формат. Введіть Прізвище та Ім'я.")
        return AWAITING_CHILD_DATA

async def finalize_setup(message, chat_id, context):
    await save_user_data(chat_id, user_sessions[chat_id])
    user_sessions[chat_id]['status'] = 'ready'
    await context.bot.send_message(chat_id, "🎉 **Налаштування завершено!**\nЗавдання додано до черги.")
    await task_queue.put(chat_id)
    logging.info(f"Користувач {chat_id} доданий до черги.")
    return ConversationHandler.END

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message_source = update.callback_query.message if update.callback_query else update.message
    chat_id = message_source.chat_id
    
    queue_size = task_queue.qsize()
    position_text = "Вашого завдання немає в черзі."
    try:
        user_position = list(task_queue._queue).index(chat_id) + 1
        position_text = f"Ваша позиція в черзі: **{user_position}**."
    except ValueError: pass
    await context.bot.send_message(chat_id, f"⚙️ Воркерів: **{WORKER_COUNT}** | 👥 В черзі: **{queue_size}**\n{position_text}")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message_source = update.callback_query.message if update.callback_query else update.message
    chat_id = message_source.chat_id
    
    if chat_id in user_sessions: del user_sessions[chat_id]
    
    temp_queue = []
    while not task_queue.empty():
        item = await task_queue.get()
        if item != chat_id:
            temp_queue.append(item)
    for item in temp_queue:
        await task_queue.put(item)

    await context.bot.send_message(chat_id, "Всі ваші завдання скасовано.")
    return ConversationHandler.END

async def show_keyboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a message with a custom keyboard."""
    keyboard = [
        [KeyboardButton("🏠 Головне меню"), KeyboardButton("📊 Дізнатися статус")],
        [KeyboardButton("📝 Нове налаштування"), KeyboardButton("▶️ Швидкий запуск")],
        [KeyboardButton("❌ Скасувати завдання"), KeyboardButton("⌨️ Сховати клавіатуру")],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    await update.message.reply_text("⌨️ Клавіатуру з командами активовано.", reply_markup=reply_markup)

async def hide_keyboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hides the custom keyboard."""
    await update.message.reply_text("Клавіатуру сховано.", reply_markup=ReplyKeyboardRemove())

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(f"Помилка в telegram-handler: {context.error}", exc_info=context.error)

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)s - %(message)s',
        handlers=[logging.FileHandler("bot_activity.log", encoding='utf-8'), logging.StreamHandler(sys.stdout)]
    )
    
    parser = argparse.ArgumentParser(description="Бот для бронювання слотів.")
    parser.add_argument('--headless', action='store_true', help="Запуск в прихованому режимі.")
    args = parser.parse_args()
    
    async def post_init(application: Application):
        os.makedirs(BASE_PROFILES_DIR, exist_ok=True)
        os.makedirs(BASE_SCREENSHOTS_DIR, exist_ok=True)
        os.makedirs(BASE_KEYS_DIR, exist_ok=True)
        os.makedirs(USER_DATA_ARCHIVES_DIR, exist_ok=True)

        user_commands = [
            BotCommand("keyboard", "⌨️ Показати клавіатуру"),
            BotCommand("menu", "🏠 Головне меню (кнопки)"),
            BotCommand("book", "▶️ Швидкий запуск (з вашими даними)"),
            BotCommand("book_new", "📝 Нове налаштування (ввід)"),
            BotCommand("status", "📊 Дізнатися статус"),
            BotCommand("cancel", "❌ Скасувати завдання"),
        ]
        await application.bot.set_my_commands(user_commands, scope=BotCommandScopeAllPrivateChats())
        logging.info("Встановлено команди за замовчуванням для всіх користувачів.")

        owner_id = os.getenv('OWNER_TELEGRAM_ID')
        if owner_id:
            admin_commands = user_commands + [BotCommand("update_lists", "🔄 Оновити списки (АДМІН)")]
            try:
                await application.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=int(owner_id)))
                logging.info(f"Встановлено розширені команди для адміністратора (ID: {owner_id}).")
            except Exception as e:
                logging.error(f"Не вдалося встановити команди для адміністратора (ID: {owner_id}): {e}")
        
        for i in range(WORKER_COUNT):
            asyncio.create_task(worker(worker_id=i + 1, app=application, headless_mode=args.headless))
            
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("book_new", book_new_command),
            CommandHandler("book", book_command),
            CommandHandler("menu", menu_command),
            MessageHandler(filters.Regex("^📝 Нове налаштування$"), book_new_command),
            MessageHandler(filters.Regex("^▶️ Швидкий запуск$"), book_command),
        ],
        states={
            MAIN_MENU: [CallbackQueryHandler(main_menu_handler, pattern='^main_')],
            SELECTING_AUTH: [CallbackQueryHandler(select_auth_method, pattern='^(diia|key|idgov)$')],
            AWAITING_KEY_FILE: [MessageHandler(filters.Document.ALL, receive_key_file)],
            AWAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
            AWAITING_DOB_GENDER: [CallbackQueryHandler(select_gender, pattern='^gender_')],
            AWAITING_DOB_CALENDAR: [CallbackQueryHandler(calendar_handler, pattern='^calendar_')],
            AWAITING_COUNTRY: [CallbackQueryHandler(select_country, pattern='^(country_|show_other_countries|other_countries_prev|other_countries_next|show_main_countries)')],
            AWAITING_INSTITUTION: [CallbackQueryHandler(select_institution, pattern='^inst_')],
            AWAITING_SERVICE: [CallbackQueryHandler(select_service, pattern='^(service_|show_)')],
            AWAITING_RECIPIENT: [CallbackQueryHandler(select_recipient, pattern='^recipient_')],
            AWAITING_CHILD_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_child_data)],
        },
        fallbacks=[CommandHandler("cancel", cancel_command), MessageHandler(filters.Regex("^❌ Скасувати завдання$"), cancel_command)],
        per_user=False, per_chat=True, per_message=False
    )

    update_lists_handler = ConversationHandler(
        entry_points=[CommandHandler("update_lists", update_lists_command)],
        states={UPDATING_LISTS: [CallbackQueryHandler(start_scraping, pattern='^start_scraping$')]},
        fallbacks=[CommandHandler("cancel", cancel_command)],
        per_message=False
    )
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(conv_handler)
    application.add_handler(update_lists_handler)
    
    application.add_handler(CommandHandler("keyboard", show_keyboard_command))
    application.add_handler(CommandHandler("hide_keyboard", hide_keyboard_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("cancel", cancel_command))

    application.add_handler(MessageHandler(filters.Regex("^🏠 Головне меню$"), menu_command))
    application.add_handler(MessageHandler(filters.Regex("^📊 Дізнатися статус$"), status_command))
    application.add_handler(MessageHandler(filters.Regex("^⌨️ Сховати клавіатуру$"), hide_keyboard_command))

    application.add_error_handler(error_handler)
    
    if not OWNER_TELEGRAM_ID:
        logging.warning("ПОПЕРЕДЖЕННЯ: OWNER_TELEGRAM_ID не встановлено.")

    logging.info(f"Бот запускається з {WORKER_COUNT} воркерами...")
    mode = "прихованому (headless)" if args.headless else "видимому"
    logging.info(f"Браузери будуть запускатися в {mode} режимі.")
    application.run_polling()

if __name__ == "__main__":
    main()