# -*- coding: utf-8 -*-

import asyncio
import json
import logging
import os
import re
import sys
import traceback
import argparse
from datetime import datetime
from dotenv import load_dotenv
import shutil
import calendar

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
from telegram.error import TelegramError, BadRequest
import re

# --- CONFIGURATION ---
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv('DEV_BOT_TOKEN')
OWNER_TELEGRAM_ID = os.getenv('OWNER_TELEGRAM_ID')
WORKER_COUNT = 2
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

class NoSlotsFoundException(Exception):
    """Custom exception for when no slots are found."""
    pass

# --- HELPER FUNCTIONS ---

async def save_screenshot(page, name, context_id, locator=None, full_page=False):
    screenshots_dir = os.path.join(BASE_SCREENSHOTS_DIR, str(context_id))
    os.makedirs(screenshots_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(screenshots_dir, f"{name}_{timestamp}.png")
    try:
        if page.is_closed():
            logging.warning(f"[ID-{context_id}] Не вдалося зберегти знімок екрана '{name}': сторінка вже закрита.")
            return None
        if locator:
            await locator.screenshot(path=path)
        else:
            await page.screenshot(path=path, full_page=full_page)
        logging.info(f"[ID-{context_id}] Знімок екрана збережено: {path}")
        return path
    except Exception as e:
        logging.error(f"[ID-{context_id}] Помилка при збереженні знімка екрана '{name}': {e}")
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

def load_user_data(user_id: int) -> dict:
    user_archive_dir = os.path.join(USER_DATA_ARCHIVES_DIR, str(user_id))
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
        logging.info(f"Завантажено персональні дані для користувача {user_id}")
        return loaded_data
    except Exception as e:
        logging.error(f"Помилка при завантаженні даних для {user_id}: {e}")
        return {}

async def save_user_data(user_id: int, user_data: dict):
    if not user_data:
        return
    user_archive_dir = os.path.join(USER_DATA_ARCHIVES_DIR, str(user_id))
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
        logging.info(f"Персональні дані для {user_id} збережено.")
    except Exception as e:
        logging.error(f"Не вдалося зберегти дані для {user_id}: {e}")

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
        try:
            await session_ended_locator.wait_for(state='visible', timeout=2000)
            logging.warning("Виявлено повідомлення 'Сесія закінчилася'. Сесія недійсна.")
            return False
        except PlaywrightTimeoutError:
            pass

        logged_in_element_selector = page.get_by_role("link", name="Запис на візит")
        await expect(logged_in_element_selector).to_be_visible(timeout=10000)
        logging.info("Активний елемент навігації знайдено. Сесія дійсна.")
        return True
    except PlaywrightTimeoutError:
        logging.warning("Авторизований елемент не знайдено. Сесія неактивна.")
        return False
    except Exception as e:
        logging.error(f"Помилка під час перевірки дійсності сесії: {e}")
        return False

async def book_available_slot(page, bot, user_id, chat_id, log_func):
    """
    Знаходить та бронює вільний слот, починаючи з поточного виду календаря.
    Ця функція об'єднує логіку пошуку з SlotVisitHelp.py.
    """
    try:
        await bot.send_message(chat_id, "🔄 Починаю пошук та бронювання...")
        log_func("Починаю пошук та бронювання за новою логікою...")

        # Спочатку перевіряємо, чи є повідомлення про відсутність часу
        no_slots_message_locator = page.locator('text="На жаль, зараз немає вільного часу"')
        try:
            await no_slots_message_locator.wait_for(state='visible', timeout=5000)
            log_func("Знайдено повідомлення про відсутність вільного часу. Слотів немає.")
            raise NoSlotsFoundException("На сторінці є повідомлення про відсутність слотів")
        except PlaywrightTimeoutError:
            # Повідомлення не знайдено, продовжуємо пошук календаря
            log_func("Повідомлення про відсутність часу не знайдено, шукаю календар.")

        # Очікуємо завантаження календаря
        try:
            await page.wait_for_selector('div.MuiCalendarPicker-root', timeout=30000)
            log_func("Календар завантажено.")
        except PlaywrightTimeoutError:
            log_func("Тайм-аут очікування календаря. Вважаємо, що слотів немає.")
            path = await save_screenshot(page, "no_calendar_found", user_id)
            if path:
                await send_telegram_photo(bot, chat_id, path, "Календар не завантажився, що вказує на відсутність вільних дат.")
            raise NoSlotsFoundException("Календар не завантажився")

        month_tabs_locator = page.locator('button[role="tab"]')
        try:
            await month_tabs_locator.first.wait_for(state='visible', timeout=15000)
            month_tabs_count = await month_tabs_locator.count()
            log_func(f"Знайдено {month_tabs_count} вкладок місяців.")
        except PlaywrightTimeoutError:
            log_func("Не знайдено вкладок місяців. Можливо, доступний лише поточний місяць.")
            month_tabs_count = 1 # Припускаємо, що є принаймні один (поточний) місяць

        # --- Основний цикл пошуку ---
        for i in range(month_tabs_count):
            if month_tabs_count > 1:
                current_month_tab = month_tabs_locator.nth(i)
                month_name = await current_month_tab.text_content()
                log_func(f"Перевіряю місяць: {month_name}")
                await current_month_tab.click()
                await page.wait_for_timeout(1000) # Даємо час на оновлення календаря

            available_days = page.locator('button.MuiPickersDay-root:not([disabled])')
            
            if await available_days.count() == 0:
                log_func("Вільних днів у цьому місяці немає.")
                continue

            log_func(f"Знайдено {await available_days.count()} потенційно вільних днів.")
            
            # Перебираємо всі доступні дні
            for day_index in range(await available_days.count()):
                day_button = available_days.nth(day_index)
                day_text = await day_button.text_content()
                log_func(f"  - Натискаю на день: {day_text}")
                await day_button.click()
                await page.wait_for_timeout(1500) # Чекаємо на появу слотів

                time_slots_locator = page.locator('div[role="listbox"] button')
                
                try:
                    await time_slots_locator.first.wait_for(state='visible', timeout=5000)
                except PlaywrightTimeoutError:
                    log_func(f"    Для дня {day_text} не з'явилися слоти часу. Продовжую.")
                    continue

                log_func(f"    Знайдено {await time_slots_locator.count()} слотів часу.")

                for slot_index in range(await time_slots_locator.count()):
                    slot_button = time_slots_locator.nth(slot_index)
                    slot_text = await slot_button.text_content()
                    log_func(f"      - Намагаюся забронювати час: {slot_text}")
                    
                    await slot_button.click()
                    
                    # Очікуємо на модальне вікно підтвердження
                    try:
                        confirm_button = page.get_by_role("button", name="Записатись на візит")
                        await confirm_button.wait_for(state='visible', timeout=10000)
                        log_func("З'явилася кнопка підтвердження 'Записатись на візит'.")
                        
                        await confirm_button.click()
                        log_func("Натиснув кнопку 'Записатись на візит'.")

                        # Очікуємо на повідомлення про успіх
                        success_locator = page.locator("h2:has-text('Успішний запис')")
                        await success_locator.wait_for(state='visible', timeout=30000)
                        
                        success_message = "✅ Успішне бронювання! Деталі у скріншоті."
                        log_func(success_message)
                        path = await save_screenshot(page, "booking_success", user_id)
                        if path:
                            await send_telegram_photo(bot, chat_id, path, caption=success_message)
                        else:
                            await bot.send_message(chat_id, success_message)

                        # Натискаємо "Зрозуміло"
                        try:
                            await page.get_by_role("button", name="Зрозуміло").click(timeout=5000)
                            log_func("Натиснув кнопку 'Зрозуміло'.")
                        except PlaywrightTimeoutError:
                            log_func("Кнопка 'Зрозуміло' не з'явилася або не була натиснута.")

                        return "Успішне бронювання"

                    except PlaywrightTimeoutError as e:
                        log_func(f"Тайм-аут після вибору слоту: {e}. Можливо, слот вже зайнятий. Пробую інший.")
                        # Якщо щось пішло не так, повертаємось до вибору дня
                        await page.reload() # Перезавантажуємо, щоб скинути стан
                        await page.wait_for_url("**/dateAndTimeVisitInfo**", timeout=60000)
                        break # Виходимо з циклу слотів і переходимо до наступного дня
                    except Exception as e:
                        log_func(f"Помилка після вибору слоту: {e}. Пробую інший.")
                        break
                else: # Якщо цикл по слотах завершився
                    continue # Переходимо до наступного дня
                break # Виходимо з циклу днів, бо ми перезавантажили сторінку
        
        # Якщо цикл по місяцях завершився без успіху
        log_func("Перевірив усі доступні місяці, але не вдалося забронювати.")
        raise NoSlotsFoundException("Не вдалося забронювати")

    except NoSlotsFoundException:
        raise # Просто передаємо виключення вище, до run_booking_session
    except PlaywrightTimeoutError as e:
        log_func(f"Тайм-аут під час пошуку слоту, розцінюю як відсутність слотів: {e}")
        raise NoSlotsFoundException("Тайм-аут під час пошуку слоту")
    except Exception as e:
        error_message = f"Критична помилка під час бронювання слоту: {e}"
        log_func(error_message)
        await bot.send_message(chat_id, f"❗️ Помилка під час бронювання: {str(e).splitlines()[0]}")
        path = await save_screenshot(page, "booking_critical_error", user_id)
        if path:
            await send_telegram_photo(bot, chat_id, path, caption="Критична помилка під час бронювання")
        raise SessionResetException(f"Критична помилка в book_available_slot: {e}")

async def run_booking_session(bot: Bot, user_id: int, user_data: dict, worker_id: int, headless_mode: bool):
    p = context = page = browser = None
    chat_id = user_data.get('chat_id')
    log_func = lambda msg: logging.info(f"[Воркер-{worker_id}][Користувач-{user_id}] {msg}")

    try:
        log_func("="*20 + " ПОЧАТОК НОВОЇ СЕСІЇ " + "="*20)
        p = await async_playwright().start()

        # Создаем персистентный профиль для пользователя
        user_profile_dir = os.path.join(BASE_PROFILES_DIR, str(user_id))
        os.makedirs(user_profile_dir, exist_ok=True)

        browser = await p.chromium.launch_persistent_context(
            user_data_dir=user_profile_dir,
            headless=headless_mode,
            channel="chrome",
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            timeout=60000,
            locale="uk-UA",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            timezone_id="Europe/Kiev"
        )
        
        # При использовании persistent_context, browser это уже BrowserContext
        context = browser
        page = await context.new_page()

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

            # Проверяем наличие поля auth_method в данных пользователя
            auth_method = user_data.get('auth_method')
            if not auth_method:
                log_func("Метод авторизации не указан в данных пользователя. Требуется повторная настройка.")
                await bot.send_message(chat_id, "❗️ Метод авторизации не найден в ваших данных. Пожалуйста, пройдите настройку заново командой /book_new")
                raise SessionResetException("Отсутствует метод авторизации в данных пользователя")
            
            if auth_method == 'diia':
                await page.get_by_role("button", name="Дія.Підпис").click()
                await page.wait_for_load_state('networkidle')
                await page.wait_for_timeout(3000)
                qr_element = page.locator("canvas, div.qr-code").first
                await qr_element.wait_for(state='visible', timeout=60000)
                qr_path = await save_screenshot(page, "diia_qr_code", user_id, locator=qr_element)
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
                qr_path = await save_screenshot(page, "bankid_qr_code", user_id, locator=qr_element)
                await send_telegram_photo(bot, chat_id, qr_path, "Відскануйте QR-код у додатку Monobank.")
                await page.wait_for_url("**/id.gov.ua/bankid-auth-callback**", timeout=180000)
                async with page.expect_navigation(url="**/e-consul.gov.ua/**"):
                    await page.locator('#btnAcceptUserDataAgreement').click()

            # Дополнительная проверка успешной загрузки
            try:
                await page.wait_for_load_state('networkidle', timeout=30000)
                await page.wait_for_load_state('domcontentloaded', timeout=30000)
                
                # Проверяем, что мы действительно авторизованы
                await page.wait_for_selector('a:has-text("Запис на візит")', timeout=20000)
                
                await bot.send_message(chat_id, "✅ Авторизація успішна. Профіль сесії збережено.")
                log_func(f"Авторизація успішна. URL: {page.url}")
            except PlaywrightTimeoutError:
                raise SessionResetException("Timeout при проверке авторизации")
        else:
            await bot.send_message(chat_id, "Сесія активна, використовую збережений профіль.")
            log_func("Сесія активна, пропускаю авторизацію.")
            
            # Дополнительная проверка активной сессии
            try:
                await page.reload(timeout=30000)
                await page.wait_for_selector('a:has-text("Запис на візит")', timeout=20000)
            except PlaywrightTimeoutError:
                raise SessionResetException("Сессия оказалась неактивной после проверки")

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
        await page.wait_for_url("**/dateAndTimeVisitInfo**", timeout=60000)
        log_func("Всі дані успішно введено.")
        await bot.send_message(chat_id, "✅ Всі дані успішно введено.")
        
        # Запускаємо одну спробу пошуку та бронювання
        log_func("Починаю спробу пошуку...")
        booking_result = await book_available_slot(page, bot, user_id, chat_id, log_func)
        
        if "Успішне" in booking_result:
            log_func("Бронювання успішне! Завершую роботу з користувачем.")
            await bot.send_message(chat_id, "🎉 **Бронювання успішне!**")
            # Успіх, виходимо з run_booking_session, воркер завершить завдання
        else:
            # Цей блок не повинен виконуватися, оскільки book_available_slot 
            # тепер викликає виключення NoSlotsFoundException
            log_func("Повернено невідомий результат з book_available_slot, перезапуск.")
            raise SessionResetException("Невідомий результат бронювання")

    except NoSlotsFoundException:
        raise # Просто передаємо виключення вище, до воркера

    except SessionResetException as e:
        log_func(f"Штатний перезапуск сесії: {e}")
        
        # Сделаем скриншот перед перезапуском
        try:
            if page and not page.is_closed():
                path = await save_screenshot(page, "session_reset", user_id)
                if path:
                    await send_telegram_photo(bot, chat_id, path, 
                        "🔄 Снимок екрана перед перезапуском сесії")
        except Exception as screenshot_error:
            log_func(f"Не вдалося зробити скриншот перед перезапуском: {screenshot_error}")
        
        # Корректно закриваем все ресурсы
        try:
            if page and not page.is_closed():
                await page.close()
            if context: 
                await context.close()
            if p: 
                await p.stop()
        except Exception as close_error:
            log_func(f"Ошибка при закрытии ресурсов: {close_error}")
        
        # Небольшая пауза перед перезапуском
        await asyncio.sleep(5)
        raise

    except Exception as e:
        error_message = f"Сталася критична помилка в run_booking_session: {e}"
        log_func(error_message)
        full_traceback = traceback.format_exc()
        logging.error("\nПОВНИЙ ТРЕЙСБЕК ПОМИЛКИ:\n" + full_traceback)
        
        path = None
        if page and not page.is_closed():
            path = await save_screenshot(page, "critical_error_screenshot", user_id)

        if chat_id:
            caption = (f"❗️ **Сталася помилка у воркері.**\n\n"
                       f"`{str(e).splitlines()[0]}`\n\n"
                       f"Завдання буде перезапущено. Дивіться скріншот для деталей.")
            
            if path:
                await send_telegram_photo(bot, chat_id, path, caption=caption)
            else:
                await bot.send_message(chat_id, caption)

        if context: await context.close()
        if p: await p.stop()
        raise SessionResetException(f"Критична помилка: {e}")
    finally:
        # Закриваємо браузер у будь-якому випадку: успіх, помилка чи перезапуск
        log_func("Спроба закрити ресурси Playwright...")
        try:
            if page and not page.is_closed():
                await page.close()
                log_func("Сторінка Playwright закрита.")
            if context:
                await context.close()
                log_func("Контекст Playwright закрито.")
            if p:
                await p.stop()
                log_func("Playwright зупинено.")
        except Exception as e:
            log_func(f"Помилка при закритті ресурсів Playwright: {e}")
        log_func("="*20 + " КІНЕЦЬ СЕСІЇ " + "="*20 + "\n")

# --- Worker and Telegram Handlers ---

async def worker(worker_id: int, app: Application, headless_mode: bool):
    logging.info(f"[Воркер-{worker_id}] запущений.")
    while True:
        try:
            user_id = await task_queue.get()
            logging.info(f"[Воркер-{worker_id}] Взяв у роботу користувача {user_id}.")
            user_data = user_sessions.get(user_id)
            if not user_data or user_data.get('status') != 'ready':
                logging.warning(f"[Воркер-{worker_id}] Дані для {user_id} не знайдено, завдання пропущено.")
                task_queue.task_done()
                continue

            max_restarts = 5
            chat_id = user_data['chat_id']
            requeued = False # Прапорець для відстеження повернення в чергу
            for attempt in range(max_restarts):
                try:
                    await run_booking_session(app.bot, user_id, user_data, worker_id, headless_mode)
                    logging.info(f"[Воркер-{worker_id}] Завдання для {user_id} успішно виконано.")
                    break
                except NoSlotsFoundException:
                    logging.info(f"[Воркер-{worker_id}] Вільних слотів для {user_id} не знайдено. Повертаю в чергу.")
                    await app.bot.send_message(chat_id, "😔 Вільних слотів не знайдено. Спробую пізніше, коли звільниться воркер.")
                    await task_queue.put(user_id) # Повертаємо в чергу
                    requeued = True # Встановлюємо прапорець
                    await asyncio.sleep(1) # Невелика затримка
                    break # Виходимо з циклу спроб
                except SessionResetException as e:
                    logging.warning(f"[Воркер-{worker_id}] Перезапуск сесії для {user_id} (спроба {attempt + 1}/{max_restarts}). Причина: {e}")
                    if attempt + 1 >= max_restarts:
                        logging.error(f"Досягнуто ліміту перезапусків для {user_id}. Завдання зупинено.")
                        await app.bot.send_message(chat_id, "Досягнуто ліміту автоматичних перезапусків. Завдання зупинено.")
                        break
                    await app.bot.send_message(chat_id, f"🔄 Сесію було скинуто. Перезапускаю... (Спроба {attempt + 1}/{max_restarts})")
                    await asyncio.sleep(15)

            logging.info(f"[Воркер-{worker_id}] Завершив роботу з користувачем {user_id}.")
            if not requeued and user_id in user_sessions:
                logging.info(f"Видаляю сесію для {user_id}, оскільки завдання не було повернуто в чергу.")
                del user_sessions[user_id]
            else:
                logging.info(f"Зберігаю сесію для {user_id}, оскільки завдання було повернуто в чергу.")
            
            task_queue.task_done()
        except Exception as e:
            logging.critical(f"Критична помилка у воркері {worker_id}: {e}\n{traceback.format_exc()}")
            task_queue.task_done()

async def update_lists_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    owner_id_str = os.getenv('OWNER_TELEGRAM_ID')

    if not owner_id_str:
        await update.message.reply_text("❗️ Команду не налаштовано: ID адміністратора не вказано.")
        logging.warning(f"Користувач {user_id} намагався виконати /update_lists, але OWNER_TELEGRAM_ID не встановлено.")
        return ConversationHandler.END

    if user_id != int(owner_id_str):
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
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    user_id = query.from_user.id

    await query.edit_message_text("Запускаю браузер з профілем адміністратора...")

    p = browser = browser_context = page = None
    try:
        p = await async_playwright().start()
        browser = await p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            timeout=60000
        )
        browser_context = await browser.new_context(
            locale="uk-UA",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            timezone_id="Europe/Kiev"
        )
        page = await browser_context.new_page()

        await page.evaluate("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        logging.info("Маскування navigator.webdriver застосовано.")

        await page.goto("https://e-consul.gov.ua/", timeout=60000, wait_until="domcontentloaded")
        await context.bot.send_message(chat_id,
            "Браузер запущено. Будь ласка, увійдіть до свого облікового запису на сайті e-consul.\n"
            "Після успішного входу, поверніться сюди.")

        await page.wait_for_url("**/messages**", timeout=300000)
        await context.bot.send_message(chat_id, "✅ Авторизація пройдена. Починаю збір даних...")

        logging.info("Перехід до форми вибору країни...")
        await page.get_by_role("link", name="Запис на візит").click()
        await page.get_by_role("link", name="Записатись на візит").click()
        await page.wait_for_url("**/applicantInfo**", timeout=60000)
        await page.locator('input[aria-label="день"]').fill("1")
        await page.locator('div[aria-label="місяць"]').click()
        await page.get_by_role("option", name="січня").click()
        await page.locator('input[aria-label="рік"]').fill("2000")
        await page.get_by_label("Жіноча").check()
        await page.get_by_role("button", name="Далі").click()
        await page.wait_for_url("**/placeOfVisitInfo**", timeout=60000)
        logging.info("Успішно перейшли до форми вибору країни.")

        await context.bot.send_message(chat_id, f"Етап 1: Читаю список країн з файлу `{DATA_FILE}`...")
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                country_list = data['countries']
            if not country_list:
                raise Exception("Список країн у файлі порожній.")
            logging.info(f"Успішно завантажено {len(country_list)} країн з файлу.")
            await context.bot.send_message(chat_id, f"✅ Список з {len(country_list)} країн завантажено.")
        except Exception as e:
            await context.bot.send_message(chat_id, f"❗️ Помилка читання файлу `{DATA_FILE}`: {e}. Оновлення неможливе.")
            raise

        await context.bot.send_message(chat_id, "Етап 2: Отримую списки консульств для кожної країни...")
        scraped_data = {'countries': country_list, 'institutions': {}}
        country_input = page.locator('input[id="country"]')
        institution_input = page.locator('input[id="consularInstitution"]')

        last_successful_country = None

        for i, country_name in enumerate(country_list):
            try:
                await country_input.fill(country_name, timeout=10000)
                await page.wait_for_timeout(500)
                await country_input.press("ArrowDown")
                await page.wait_for_timeout(200)
                await country_input.press("Enter")

                await expect(institution_input).to_be_enabled(timeout=5000)

                institutions = []
                try:
                    await institution_input.click(force=True, timeout=5000)
                    institution_options_locator = page.locator('div[class*="MuiAutocomplete-popper"] li.MuiAutocomplete-option')
                    await institution_options_locator.first.wait_for(state='visible', timeout=10000)
                    institutions = sorted(await institution_options_locator.all_text_contents())
                    await page.locator('body').click()
                except PlaywrightTimeoutError:
                    logging.info(f"Для країни '{country_name}' установ не знайдено (таймаут очікування списку). Пропускаю.")
                    institutions = []

                scraped_data['institutions'][country_name] = institutions
                if institutions:
                    last_successful_country = {"name": country_name, "institution": institutions[0]}

                logging.info(f"({i + 1}/{len(country_list)}) Зібрано {len(institutions)} установ для: {country_name}")

                await institution_input.clear()
                await country_input.clear()

                if (i + 1) % 25 == 0:
                    await context.bot.send_message(chat_id, f"Оброблено {i + 1}/{len(country_list)} країн...")

            except Exception as e:
                logging.error(f"Не вдалося обробити країну '{country_name}': {e}. Пропускаю.")
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_url("**/placeOfVisitInfo**", timeout=60000)
                country_input = page.locator('input[id="country"]')
                institution_input = page.locator('input[id="consularInstitution"]')

        await context.bot.send_message(chat_id, "Етап 3: Отримую повний список послуг...")

        next_button = page.get_by_role("button", name="Далі")
        if not await next_button.is_enabled():
            logging.warning("Кнопка 'Далі' неактивна. Спроба 'розбудити' сторінку, вибравши останню країну.")
            if last_successful_country:
                await country_input.fill(last_successful_country["name"])
                await page.get_by_role("option", name=last_successful_country["name"], exact=True).click()
                await expect(institution_input).to_be_enabled(timeout=10000)
                await institution_input.fill(last_successful_country["institution"])
                await page.get_by_role("option", name=last_successful_country["institution"], exact=True).click()
                await expect(next_button).to_be_enabled(timeout=10000)
            else:
                raise Exception("Кнопка 'Далі' неактивна, і немає даних для її активації.")

        await next_button.click()
        await page.wait_for_url("**/consularServiceInfo**", timeout=60000)

        service_input = page.locator('input[id="service"]')
        await service_input.click(timeout=10000)
        await page.get_by_role("option").first.wait_for(state='visible', timeout=15000)

        found_services = set()
        no_new_services_streak = 0
        for _ in range(250):
            current_options = await page.get_by_role("option").all()
            initial_count = len(found_services)
            for option in current_options:
                text = await option.text_content()
                if text:
                    found_services.add(text.strip())

            if len(found_services) > initial_count:
                no_new_services_streak = 0
            else:
                no_new_services_streak += 1

            if no_new_services_streak >= 15:
                logging.info("Прокрутка завершена: список послуг перестав оновлюватися.")
                break

            await service_input.press("ArrowDown")
            await page.wait_for_timeout(50)

        all_available_services = sorted(list(found_services))
        scraped_data['services'] = all_available_services
        logging.info(f"Успішно зібрано {len(all_available_services)} унікальних послуг.")

        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(scraped_data, f, ensure_ascii=False, indent=4)

        await context.bot.send_message(chat_id, f"🎉 **Готово!**\nДовідники успішно оновлено.\n"
                                        f"Використано країн з файлу: {len(country_list)}\n"
                                        f"Знайдено країн з установами: {len(scraped_data['institutions'])}\n"
                                        f"Знайдено консульських послуг: {len(all_available_services)}")

    except Exception as e:
        logging.error(f"Сталася критична помилка в start_scraping: {e}", exc_info=True)
        if page and not page.is_closed():
             path = await save_screenshot(page, "critical_scraping_error", user_id)
             await send_telegram_photo(context.bot, chat_id, path, "Критична помилка під час збору даних")
        await context.bot.send_message(chat_id, f"❗️ Сталася критична помилка: {str(e).splitlines()[0]}")
    finally:
        if browser_context: await browser_context.close()
        if browser and browser.is_connected(): await browser.close()
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
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if user_id in list(task_queue._queue) or (user_sessions.get(user_id) and user_sessions[user_id].get('status') == 'ready'):
        await context.bot.send_message(chat_id, "❗️ Ваше завдання вже в черзі. Для скасування використовуйте /cancel.")
        return ConversationHandler.END

    user_sessions[user_id] = {'user_id': user_id, 'chat_id': chat_id}
    keyboard = [
        [InlineKeyboardButton("Дія.Підпис", callback_data='diia')],
        [InlineKeyboardButton("Особистий ключ (файл)", callback_data='key')],
        [InlineKeyboardButton("ID.GOV.UA (BankID)", callback_data='idgov')],
    ]
    message_source = update.callback_query.message if update.callback_query else update.message
    await message_source.reply_text(
        "**Крок 1/5: Метод входу**\n\nВиберіть метод автентифікації:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECTING_AUTH

async def book_new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logging.info(f"Користувач {get_user_info(update)} виконав команду /book_new")
    return await start_new_booking_flow(update, context)

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if user_id in list(task_queue._queue) or (user_sessions.get(user_id) and user_sessions[user_id].get('status') == 'ready'):
        await update.message.reply_text("❗️ Ваше завдання вже в черзі. Для скасування використовуйте /cancel.")
        return ConversationHandler.END

    # Спочатку спробуємо завантажити дані з файлу
    persisted_data = load_user_data(user_id)
    
    # Якщо в поточній сесії є новіші дані, оновимо їх перед збереженням
    if user_id in user_sessions:
        persisted_data.update(user_sessions[user_id])

    if not persisted_data:
        await update.message.reply_text("Збережені дані не знайдено. Починаю нове налаштування...")
        return await start_new_booking_flow(update, context)

    # Зберігаємо актуальні дані перед постановкою в чергу
    await save_user_data(user_id, persisted_data)
    
    # Завантажуємо збережені дані для чистоти процесу
    final_user_data = load_user_data(user_id)

    user_sessions[user_id] = final_user_data
    user_sessions[user_id]['user_id'] = user_id
    user_sessions[user_id]['chat_id'] = chat_id
    user_sessions[user_id]['status'] = 'ready'
    
    await task_queue.put(user_id)
    logging.info(f"Користувач {user_id} доданий до черги зі збереженими даними.")
    await update.message.reply_text("✅ **Використовую збережені дані.**\nЗавдання додано до черги.")
    return ConversationHandler.END

async def select_auth_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    auth_method = query.data
    user_sessions[user_id]['auth_method'] = auth_method
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
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    document = update.message.document
    keys_dir = os.path.join(BASE_KEYS_DIR, str(user_id))
    os.makedirs(keys_dir, exist_ok=True)
    file_path = os.path.join(keys_dir, document.file_name)
    file = await document.get_file()
    await file.download_to_drive(file_path)
    user_sessions[user_id]['key_file_path'] = file_path
    user_sessions[user_id]['key_file_name'] = document.file_name
    await update.message.reply_text("✅ Файл отримано. Тепер введіть пароль до цього ключа.")
    return AWAITING_PASSWORD

async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    user_sessions[user_id]['key_password'] = update.message.text
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
    user_id = query.from_user.id
    user_sessions[user_id]['gender'] = query.data.split('_')[1]
    now = datetime.now()
    await query.edit_message_text("Стать обрано. Тепер виберіть вашу дату народження:",
                                  reply_markup=await generate_calendar_keyboard(now.year, now.month))
    return AWAITING_DOB_CALENDAR

async def calendar_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat.id
    action, *params = query.data.split('_')

    if action != "calendar":
        return AWAITING_DOB_CALENDAR

    command = params[0]

    try:
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
            user_sessions[user_id]['date_of_birth'] = selected_date_str
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

            context.user_data['main_countries'] = main_countries
            context.user_data['other_countries'] = other_countries

            keyboard = [[InlineKeyboardButton(country, callback_data=f"country_{country}")] for country in main_countries]
            if other_countries:
                keyboard.append([InlineKeyboardButton("Інші країни >>", callback_data="show_other_countries")])

            await context.bot.send_message(chat_id, "**Крок 3/5: Місцезнаходження**\n\nВиберіть країну:",
                                           reply_markup=InlineKeyboardMarkup(keyboard))
            return AWAITING_COUNTRY

    except BadRequest as e:
        if "Message is not modified" in str(e):
            logging.info("Ignored BadRequest: Message is not modified.")
        else:
            raise

    return AWAITING_DOB_CALENDAR

async def select_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
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
        context.user_data['other_countries_page'] = 0
        await query.edit_message_reply_markup(reply_markup=get_paginated_menu(context.user_data['other_countries'], 0, "other_countries", "show_main_countries"))
        return AWAITING_COUNTRY

    if query.data in ["other_countries_prev", "other_countries_next"]:
        page = context.user_data.get('other_countries_page', 0)
        page = max(0, page - 1) if query.data.endswith("_prev") else page + 1
        context.user_data['other_countries_page'] = page
        await query.edit_message_reply_markup(reply_markup=get_paginated_menu(context.user_data['other_countries'], page, "other_countries", "show_main_countries"))
        return AWAITING_COUNTRY

    if query.data == "show_main_countries":
        main_countries_menu_keyboard = []
        for country in context.user_data['main_countries']:
            main_countries_menu_keyboard.append([InlineKeyboardButton(country, callback_data=f"country_{country}")])
        if context.user_data['other_countries']:
            main_countries_menu_keyboard.append([InlineKeyboardButton("Інші країни >>", callback_data="show_other_countries")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(main_countries_menu_keyboard))
        return AWAITING_COUNTRY

    country_name = query.data.split('_', 1)[1]
    user_sessions[user_id]['country'] = country_name

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
    user_id = query.from_user.id

    with open(DATA_FILE, 'r', encoding='utf-8') as f: data = json.load(f)
    country = user_sessions[user_id]['country']
    institutions_list = data['institutions'].get(country, [])

    inst_index = int(query.data.split('_', 1)[1])
    inst_name = institutions_list[inst_index]
    user_sessions[user_id]['institution'] = inst_name

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

    context.user_data['all_services_map'] = {f"service_{i}": text for i, text in enumerate(main_service_options + other_service_options)}

    keyboard = []
    for opt in main_service_options:
        cb_data = next((key for key, value in context.user_data['all_services_map'].items() if value == opt), None)
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
    user_id = query.from_user.id

    all_services_map = context.user_data.get('all_services_map', {})
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
            user_sessions[user_id]['service_type'] = selected_service
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
    user_id = query.from_user.id
    recipient = query.data.split('_')[1]
    user_sessions[user_id]['service_recipient'] = recipient
    if recipient == 'child':
        await query.edit_message_text("Введіть ПІБ дитини у форматі: Прізвище Ім'я По-батькові")
        return AWAITING_CHILD_DATA
    else:
        return await finalize_setup(query.message, user_id, context)

async def receive_child_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    parts = update.message.text.split()
    if len(parts) >= 2:
        user_sessions[user_id]['child_surname'] = parts[0]
        user_sessions[user_id]['child_firstname'] = parts[1]
        user_sessions[user_id]['child_middlename'] = " ".join(parts[2:])
        return await finalize_setup(update.message, user_id, context)
    else:
        await update.message.reply_text("Невірний формат. Введіть Прізвище та Ім'я.")
        return AWAITING_CHILD_DATA

async def finalize_setup(message, user_id, context):
    chat_id = message.chat_id
    await save_user_data(user_id, user_sessions[user_id])
    user_sessions[user_id]['status'] = 'ready'
    await context.bot.send_message(chat_id, "🎉 **Налаштування завершено!**\nЗавдання додано до черги.")
    await task_queue.put(user_id)
    logging.info(f"Користувач {user_id} доданий до черги.")
    return ConversationHandler.END

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message_source = update.callback_query.message if update.callback_query else update.message
    user_id = update.effective_user.id
    chat_id = message_source.chat_id

    queue_size = task_queue.qsize()
    position_text = "Вашого завдання немає в черзі."
    try:
        user_position = list(task_queue._queue).index(user_id) + 1
        position_text = f"Ваша позиція в черзі: **{user_position}**."
    except ValueError: pass
    await context.bot.send_message(chat_id, f"⚙️ Воркерів: **{WORKER_COUNT}** | 👥 В черзі: **{queue_size}**\n{position_text}")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message_source = update.callback_query.message if update.callback_query else update.message
    user_id = update.effective_user.id
    chat_id = message_source.chat_id

    if user_id in user_sessions: del user_sessions[user_id]

    temp_queue = []
    while not task_queue.empty():
        item = await task_queue.get()
        if item != user_id:
            temp_queue.append(item)
    for item in temp_queue:
        await task_queue.put(item)

    await context.bot.send_message(chat_id, "Всі ваші завдання скасовано.")
    return ConversationHandler.END

async def show_keyboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [KeyboardButton("🏠 Головне меню"), KeyboardButton("📊 Дізнатися статус")],
        [KeyboardButton("📝 Нове налаштування"), KeyboardButton("▶️ Швидкий запуск")],
        [KeyboardButton("❌ Скасувати завдання"), KeyboardButton("⌨️ Сховати клавіатуру")],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    await update.message.reply_text("⌨️ Клавіатуру з командами активовано.", reply_markup=reply_markup)

async def hide_keyboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Клавіатуру сховано.", reply_markup=ReplyKeyboardRemove())

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, BadRequest) and "Message is not modified" in str(context.error):
        logging.info("Ignored BadRequest: Message is not modified.")
        return
    logging.error(f"Помилка в telegram-handler: {context.error}", exc_info=context.error)

async def forward_to_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles user messages and forwards them to the admin, and handles admin replies."""
    user = update.effective_user
    message = update.effective_message
    owner_id_str = os.getenv('OWNER_TELEGRAM_ID')

    if not owner_id_str:
        logging.warning("OWNER_TELEGRAM_ID is not set. Message forwarding is disabled.")
        return

    try:
        owner_id = int(owner_id_str)
    except (ValueError, TypeError):
        logging.error(f"Invalid OWNER_TELEGRAM_ID: {owner_id_str}. Must be an integer.")
        return

    # If the message is from the admin and is a reply
    if user.id == owner_id and message.reply_to_message and message.reply_to_message.text:
        # Try to find the original user's ID in the replied-to message
        match = re.search(r"\[ID: (\d+)\]", message.reply_to_message.text)
        if match:
            original_user_id = int(match.group(1))
            try:
                await context.bot.send_message(chat_id=original_user_id, text=f"Відповідь від адміністратора:\n\n{message.text}")
                await message.reply_text("✅ Вашу відповідь надіслано користувачу.")
                logging.info(f"Admin replied to user {original_user_id}")
            except TelegramError as e:
                await message.reply_text(f"❗️ Не вдалося надіслати відповідь. Помилка: {e}")
                logging.error(f"Failed to send admin reply to {original_user_id}: {e}")
    # If the message is from a regular user
    elif user.id != owner_id:
        forward_text = (
            f"Нове повідомлення від: {user.full_name} (@{user.username or 'N/A'})\n"
            f"[ID: {user.id}]\n\n"
            f"{message.text}"
        )
        try:
            await context.bot.send_message(chat_id=owner_id, text=forward_text)
            logging.info(f"Forwarded message from user {user.id} to admin.")
        except TelegramError as e:
            logging.error(f"Failed to forward message to admin: {e}")

async def setup_bot_commands(bot: Bot):
    """Sets up the default and admin bot commands."""
    user_commands = [
        BotCommand("keyboard", "⌨️ Показати клавіатуру"),
        BotCommand("menu", "🏠 Головне меню (кнопки)"),
        BotCommand("book", "▶️ Швидкий запуск (з вашими даними)"),
        BotCommand("book_new", "📝 Нове налаштування (ввід)"),
        BotCommand("status", "📊 Дізнатися статус"),
        BotCommand("cancel", "❌ Скасувати завдання"),
    ]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeAllPrivateChats())
    logging.info("Встановлено команди за замовчуванням для всіх користувачів.")

    owner_id = os.getenv('OWNER_TELEGRAM_ID')
    if owner_id:
        admin_commands = user_commands + [BotCommand("update_lists", "🔄 Оновити списки (АДМІН)")]
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=int(owner_id)))
            logging.info(f"Встановлено розширені команди для адміністратора (ID: {owner_id}).")
        except Exception as e:
            logging.error(f"Не вдалося встановити команди для адміністратора (ID: {owner_id}): {e}")

async def main() -> None:
    """Sets up and runs the bot."""
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)s - %(message)s',
        handlers=[logging.FileHandler("bot_activity.log", encoding='utf-8'), logging.StreamHandler(sys.stdout)]
    )

    parser = argparse.ArgumentParser(description="Бот для бронювання слотів.")
    parser.add_argument('--headless', action='store_true', help="Запуск в прихованому режимі.")
    args = parser.parse_args()

    # Create directories
    os.makedirs(BASE_PROFILES_DIR, exist_ok=True)
    os.makedirs(BASE_SCREENSHOTS_DIR, exist_ok=True)
    os.makedirs(BASE_KEYS_DIR, exist_ok=True)
    os.makedirs(USER_DATA_ARCHIVES_DIR, exist_ok=True)

    # Build Application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Conversation Handlers
    conv_handler = ConversationHandler(
entry_points=[
    CommandHandler("book_new", book_new_command),
    CommandHandler("book", book_command),
    CommandHandler("menu", menu_command),
    MessageHandler(filters.Regex("^📝 Нове налаштування$"), book_new_command),
    MessageHandler(filters.Regex("^▶️ Швидкий запуск$"), book_command),
    CallbackQueryHandler(main_menu_handler, pattern='^main_book_new$')
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
        per_user=True, per_chat=True
    )

    update_lists_handler = ConversationHandler(
        entry_points=[CommandHandler("update_lists", update_lists_command)],
        states={UPDATING_LISTS: [CallbackQueryHandler(start_scraping, pattern='^start_scraping$')]},
        fallbacks=[CommandHandler("cancel", cancel_command)],
        per_user=True, per_chat=True
    )

    # Add handlers
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
    
    # Handler for forwarding messages to admin (must be after other text handlers)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, forward_to_admin_handler))
    
    application.add_error_handler(error_handler)

    # Start workers
    worker_tasks = []
    for i in range(WORKER_COUNT):
        task = asyncio.create_task(worker(i + 1, application, args.headless))
        worker_tasks.append(task)

    logging.info(f"Бот запускається з {WORKER_COUNT} воркерами...")
    mode = "прихованому (headless)" if args.headless else "видимому"
    logging.info(f"Браузери будуть запускатися в {mode} режимі.")
    if not OWNER_TELEGRAM_ID:
        logging.warning("ПОПЕРЕДЖЕННЯ: OWNER_TELEGRAM_ID не встановлено.")

    # Initialize and start the application
    await application.initialize()
    await application.start()
    
    # Set up bot commands
    await setup_bot_commands(application.bot)
    
    # Start updater
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    
    # Run bot and workers
    try:
        logging.info("Бот запускається...")
        await asyncio.gather(*worker_tasks)
    except (KeyboardInterrupt, SystemExit):
        logging.info("Отримано сигнал зупинки. Завершую роботу...")
    finally:
        logging.info("Зупинка воркерів...")
        for task in worker_tasks:
            task.cancel()
        # Wait for tasks to finish cancelling
        await asyncio.gather(*worker_tasks, return_exceptions=True)
        logging.info("Воркери зупинено.")
        
        # Stop the application
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    # The try/except block for KeyboardInterrupt is now within the main async function
    # to allow for graceful async cleanup.
    asyncio.run(main())