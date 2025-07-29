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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot, BotCommand
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

# --- КОНФИГУРАЦИЯ ---
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv('DEV_BOT_TOKEN')
OWNER_TELEGRAM_ID = os.getenv('OWNER_TELEGRAM_ID')
WORKER_COUNT = 2
BASE_SESSIONS_DIR = 'sessions_data'
BASE_SCREENSHOTS_DIR = 'screenshots_data'
BASE_KEYS_DIR = 'user_keys'
USER_DATA_ARCHIVES_DIR = 'user_data_archives'
DATA_FILE = 'countries_data.json'

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
task_queue = asyncio.Queue()
user_sessions = {}


class SessionResetException(Exception):
    """Исключение, вызываемое при сбросе сессии или другой критической ошибке, требующей перезапуска."""
    pass

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

async def save_screenshot(page, name, chat_id, locator=None, full_page=False):
    screenshots_dir = os.path.join(BASE_SCREENSHOTS_DIR, str(chat_id))
    os.makedirs(screenshots_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(screenshots_dir, f"{name}_{timestamp}.png")
    try:
        if page.is_closed():
            logging.warning(f"[Чат-{chat_id}] Не удалось сохранить скриншот '{name}': страница уже закрыта.")
            return None
        if locator:
            await locator.screenshot(path=path)
        else:
            await page.screenshot(path=path, full_page=full_page)
        logging.info(f"[Чат-{chat_id}] Скриншот сохранен: {path}")
        return path
    except Exception as e:
        logging.error(f"[Чат-{chat_id}] Ошибка при сохранении скриншота '{name}': {e}")
        return None

async def send_telegram_photo(bot: Bot, chat_id: int, photo_path: str, caption=""):
    if not photo_path or not os.path.exists(photo_path):
        return
    try:
        with open(photo_path, 'rb') as photo_file:
            await bot.send_photo(chat_id=chat_id, photo=photo_file, caption=caption)
        logging.info(f"[Чат-{chat_id}] Скриншот '{photo_path}' успешно отправлен.")
        os.remove(photo_path)
    except Exception as e:
        logging.error(f"[Чат-{chat_id}] Ошибка при отправке фото '{photo_path}': {e}")

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
        logging.info(f"Загружены персональные данные для пользователя {chat_id}")
        return loaded_data
    except Exception as e:
        logging.error(f"Ошибка при загрузке данных для {chat_id}: {e}")
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
        logging.info(f"Персональные данные для {chat_id} сохранены.")
    except Exception as e:
        logging.error(f"Не удалось сохранить данные для {chat_id}: {e}")

async def generate_year_selection_keyboard(base_year: int, original_month: int) -> InlineKeyboardMarkup:
    keyboard = [[InlineKeyboardButton("Выберите год", callback_data="calendar_ignore")]]
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
    month_names = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
    keyboard = [[InlineKeyboardButton(f"Выберите месяц для {year} года", callback_data="calendar_ignore")]]
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
    month_names_ru = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
    cal = calendar.monthcalendar(year, month)
    keyboard = []
    header_buttons = [
        InlineKeyboardButton(f"{month_names_ru[month]}", callback_data=f"calendar_show_months_{year}_{month}"),
        InlineKeyboardButton(f"{year}", callback_data=f"calendar_show_years_{year}_{month}")
    ]
    keyboard.append(header_buttons)
    keyboard.append([InlineKeyboardButton(day, callback_data="calendar_ignore") for day in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]])
    for week in cal:
        row = [InlineKeyboardButton(str(day) if day != 0 else " ", callback_data=f"calendar_select_{year}_{month}_{day}") for day in week]
        keyboard.append(row)
    prev_month, prev_year = (month - 1, year) if month > 1 else (12, year - 1)
    next_month, next_year = (month + 1, year) if month < 12 else (1, year + 1)
    nav_buttons = [
        InlineKeyboardButton("<", callback_data=f"calendar_nav_{prev_year}_{prev_month}"),
        InlineKeyboardButton("Сегодня", callback_data=f"calendar_nav_{datetime.now().year}_{datetime.now().month}"),
        InlineKeyboardButton(">", callback_data=f"calendar_nav_{next_year}_{next_month}")
    ]
    keyboard.append(nav_buttons)
    return InlineKeyboardMarkup(keyboard)

# --- НОВЫЙ БЛОК АВТОМАТИЗАЦИИ (ОСНОВАН НА SLOTVISITHELP.PY) ---

async def check_session_validity(page):
    """Проверяет, активна ли сессия на сайте e-consul."""
    try:
        # Проверяем наличие сообщения об окончании сессии
        session_ended_locator = page.locator('text="Сесія закінчилася"')
        if await session_ended_locator.is_visible(timeout=2000):
            logging.warning("Обнаружено сообщение 'Сесія закінчилася'. Сессия невалидна.")
            return False

        # Проверяем наличие элемента, который виден только авторизованному пользователю
        logged_in_element_selector = page.get_by_role("link", name="Запис на візит")
        await expect(logged_in_element_selector).to_be_visible(timeout=5000)
        logging.info("Активный элемент навигации найден. Сессия валидна.")
        return True
    except PlaywrightTimeoutError:
        logging.warning("Авторизованный элемент не найден. Сессия неактивна.")
        return False
    except Exception as e:
        logging.error(f"Ошибка при проверке валидности сессии: {e}")
        return False

async def book_available_slot(page, bot, chat_id, log_func):
    """Ищет и бронирует первый доступный слот."""
    log_func("Начало автоматического бронирования слота.")
    try:
        month_container_locator = page.locator('div[class*="scrollbar-container"]')
        await month_container_locator.wait_for(state='visible', timeout=20000)
        month_tabs_locator = month_container_locator.get_by_text(re.compile(r"^\S+ \d{4}$"))
        month_tabs_count = await month_tabs_locator.count()
        if month_tabs_count == 0:
             raise Exception("Не удалось найти вкладки с месяцами.")
        log_func(f"Вкладки месяцев успешно найдены: {month_tabs_count} шт.")
        for i in range(month_tabs_count):
            current_month_tab = page.locator('div[class*="scrollbar-container"]').get_by_text(re.compile(r"^\S+ \d{4}$")).nth(i)
            month_name = await current_month_tab.text_content()
            log_func(f"Проверяю месяц: {month_name}")
            await current_month_tab.click(timeout=15000)
            await page.wait_for_timeout(random.randint(1500, 2500))
            await page.wait_for_load_state('networkidle', timeout=60000)
            base_locator = page.locator('div:has-text("СЛОТІВ")')
            available_week_locator = base_locator.filter(has_not_text="0 ВІЛЬНИХ")
            if await available_week_locator.count() == 0:
                continue
            log_func(f"!!! Найдена неделя со слотами в месяце '{month_name}'!!!")
            week_block = available_week_locator.first
            path = await save_screenshot(page, f"week_with_slots_{month_name}", chat_id)
            await send_telegram_photo(bot, chat_id, path, f"Найдена неделя со слотами в месяце {month_name}.")
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
                log_func(f"В неделе '{month_name}' не оказалось свободных слотов после проверки. Возвращаюсь.")
                await page.get_by_role("button", name="Назад").click(timeout=10000)
                await page.wait_for_load_state('networkidle', timeout=15000)
                continue
            slot_time_text = await first_available_slot.text_content()
            log_func(f"Найден свободный слот: {slot_time_text}. Выбираю...")
            await first_available_slot.click(timeout=15000)
            confirm_button_selector = 'button#finish-button'
            await page.locator(confirm_button_selector).wait_for(state='visible', timeout=15000)
            log_func("Нажимаю кнопку 'Підтвердити'...")
            path = await save_screenshot(page, "final_confirmation", chat_id)
            await send_telegram_photo(bot, chat_id, path, "Экран перед финальным подтверждением.")
            await page.locator(confirm_button_selector).click()
            success_locator = page.locator('text="Ваш візит успішно заброньовано"')
            await expect(success_locator).to_be_visible(timeout=60000)
            final_message = "!!! УСПЕХ: Визит успешно забронирован! Проверьте почту и страницу 'Мої візити'."
            log_func(final_message)
            await bot.send_message(chat_id, f"🎉 **УСПЕХ!** 🎉\n{final_message}")
            path = await save_screenshot(page, "booking_confirmed", chat_id)
            await send_telegram_photo(bot, chat_id, path, "Визит успешно забронирован!")
            return "Успешное автоматическое бронирование."
        log_func("Поиск завершен. Не найдено доступных для бронирования слотов.")
        return "Не удалось забронировать слот."
    except Exception as e:
        error_message = f"Критическая ошибка во время автоматического бронирования: {e}"
        log_func(error_message)
        await bot.send_message(chat_id, f"❗️ Ошибка при бронировании: {e}")
        path = await save_screenshot(page, "booking_error", chat_id)
        await send_telegram_photo(bot, chat_id, path, "Ошибка на этапе бронирования.")
        raise SessionResetException(f"Ошибка при бронировании: {e}")

async def run_booking_session(bot: Bot, chat_id: int, user_data: dict, worker_id: int, headless_mode: bool):
    """
    ОСНОВНАЯ ФУНКЦИЯ-ВОРКЕР, ПЕРЕПИСАННАЯ НА ОСНОВЕ ЛОГИКИ SLOTVISITHELP.PY
    """
    p = browser = context = page = None
    log_func = lambda msg: logging.info(f"[Воркер-{worker_id}][Чат-{chat_id}] {msg}")
    storage_state_path = os.path.join(BASE_SESSIONS_DIR, f"session_{chat_id}.json")
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

    try:
        log_func("="*20 + " НАЧАЛО НОВОЙ СЕССИИ " + "="*20)
        p = await async_playwright().start()
        browser = await p.chromium.launch(
            headless=headless_mode,
            channel="chrome",
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            timeout=60000
        )
        
        context_args = {'locale': "uk-UA", 'user_agent': USER_AGENT, 'timezone_id': "Europe/Kiev", 'viewport': None}
        if os.path.exists(storage_state_path):
            log_func("Найден файл сессии. Загружаю...")
            context_args['storage_state'] = storage_state_path
        
        context = await browser.new_context(**context_args)
        page = await context.new_page()

        # --- КЛЮЧЕВОЙ МОМЕНТ: МАСКИРОВКА WEBRDRIVER ---
        await page.evaluate("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        log_func("Маскировка navigator.webdriver применена.")

        await page.goto("https://e-consul.gov.ua/", timeout=120000, wait_until="load")
        log_func(f"Страница загружена. URL: {page.url}")

        is_logged_in = await check_session_validity(page)
        if not is_logged_in:
            log_func("Сессия неактивна. Начинаю процесс авторизации...")
            await bot.send_message(chat_id, "Сессия неактивна. Начинаю авторизацию...")
            
            if "id.e-consul.gov.ua" not in page.url:
                 await page.goto("https://id.e-consul.gov.ua/", timeout=60000)
            
            auth_method = user_data['auth_method']
            if auth_method == 'diia':
                # ... (логика авторизации без изменений) ...
                await page.get_by_role("button", name="Дія.Підпис").click()
                await page.wait_for_load_state('networkidle')
                await page.wait_for_timeout(3000) 
                qr_element = page.locator("canvas, div.qr-code").first
                await qr_element.wait_for(state='visible', timeout=60000)
                qr_path = await save_screenshot(page, "diia_qr_code", chat_id, locator=qr_element)
                await send_telegram_photo(bot, chat_id, qr_path, "Відскануйте QR-код у застосунку 'Дія' для авторизації.")
                await bot.send_message(chat_id, "Ожидаю подтверждения в 'Дія'. У вас есть 3 минуты.")
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
                await send_telegram_photo(bot, chat_id, qr_path, "Отсканируйте QR-код в приложении Monobank.")
                await page.wait_for_url("**/id.gov.ua/bankid-auth-callback**", timeout=180000)
                async with page.expect_navigation(url="**/e-consul.gov.ua/**"):
                    await page.locator('#btnAcceptUserDataAgreement').click()
            
            await page.wait_for_load_state('networkidle')
            await bot.send_message(chat_id, "✅ Авторизация прошла успешно.")
            log_func(f"Авторизация прошла успешно. URL: {page.url}")
            await context.storage_state(path=storage_state_path)
            log_func(f"Состояние сессии сохранено в {storage_state_path}")
        else:
            await bot.send_message(chat_id, "Сессия активна, пропускаю авторизацию.")
            log_func("Сессия активна, пропускаю авторизацию.")

        # --- НАЧАЛО ОСНОВНОГО ЦИКЛА ПОИСКА ---
        await bot.send_message(chat_id, "Перехожу к созданию записи и заполнению данных...")
        await page.get_by_role("link", name="Запис на візит").click()
        await page.get_by_role("link", name="Записатись на візит").click()
        await page.wait_for_url("**/applicantInfo**", timeout=60000)

        # Заполнение данных заявителя
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
        
        # Заполнение места визита
        await page.locator('input[id="country"]').fill(user_data['country'])
        await page.locator(f"li:has-text(\"{user_data['country']}\")").click()
        await page.wait_for_timeout(2000)
        await page.locator('input[id="consularInstitution"]').fill(user_data['institution'])
        await page.locator(f"li:has-text(\"{user_data['institution']}\")").click()
        await page.get_by_role("button", name="Далі").click()
        await page.wait_for_url("**/consularServiceInfo**", timeout=60000)

        # Заполнение данных об услуге
        service_input = page.locator('input[id="service"]')
        await service_input.click()
        await service_input.fill(user_data['service_type'])
        await page.wait_for_timeout(500)
        await page.get_by_role("option", name=user_data['service_type'], exact=True).click()
        recipient_map = {'self': "Для себе", 'child': "Для своєї дитини, підопічного"}
        await page.get_by_label(recipient_map[user_data['service_recipient']]).check()
        if user_data['service_recipient'] == 'child':
            await page.locator('input[aria-label="Прізвище"]').fill(user_data['child_surname'])
            await page.locator('input[aria-label=" Ім\'я"]').fill(user_data['child_firstname'])
            await page.locator('input[aria-label="По батькові"]').fill(user_data['child_middlename'])
        await page.get_by_role("button", name="Далі").click()
        log_func("Все данные успешно введены.")
        await bot.send_message(chat_id, "✅ Все данные успешно введены.")
        await page.wait_for_url("**/dateAndTimeVisitInfo**", timeout=60000)
        
        # --- ЦИКЛ ПОИСКА СЛОТОВ ---
        await bot.send_message(chat_id, "Начинаю поиск свободных слотов. Это может занять много времени...")
        log_func("Начинаю цикл поиска слотов.")
        
        max_retries = 99999
        for i in range(max_retries):
            log_func(f"Попытка поиска слотов #{i + 1}")
            await expect(page.locator('text="Відбувається пошук активних слотів..."')).to_be_hidden(timeout=120000)
            await page.wait_for_timeout(5000)

            if await page.locator('div[class*="scrollbar-container"]').is_visible():
                log_func("!!! КАЛЕНДАРЬ НАЙДЕН! Слоты доступны !!!")
                await bot.send_message(chat_id, "🔥 **НАЙДЕНЫ СЛОТЫ!** 🔥\nНачинаю бронирование...")
                result = await book_available_slot(page, bot, chat_id, log_func)
                if "Успешное" in result:
                    return # Успешно выходим
                else: # Если бронирование не удалось
                    raise SessionResetException("Бронирование не удалось, перезапускаю сессию для новой попытки.")
            else:
                log_func("Слотов нет. Перезагружаю страницу...")
                await page.reload(wait_until='networkidle')
                await asyncio.sleep(random.randint(10, 20))

    except SessionResetException as e:
        log_func(f"Штатный перезапуск сессии: {e}")
        raise  # Передаем исключение выше, чтобы worker его поймал
    except Exception as e:
        error_message = f"Произошла критическая ошибка в run_booking_session: {e}"
        log_func(error_message)
        full_traceback = traceback.format_exc()
        logging.error("\nПОЛНЫЙ ТРЕЙСБЕК ОШИБКИ:\n" + full_traceback)
        await bot.send_message(chat_id, f"❗️ **Произошла ошибка в работе воркера.**\n\n`{str(e)}`\n\nЗадача будет перезапущена.")
        if page and not page.is_closed():
            await save_screenshot(page, "error_screenshot", chat_id)
        raise SessionResetException(f"Критическая ошибка: {e}") # Инициируем перезапуск
    finally:
        if context: await context.close()
        if browser: await browser.close()
        if p: await p.stop()
        log_func("="*20 + " КОНЕЦ СЕССИИ " + "="*20 + "\n")


# --- Worker и Telegram Handlers (без значительных изменений, кроме логирования) ---

async def worker(worker_id: int, app: Application, headless_mode: bool):
    logging.info(f"[Воркер-{worker_id}] запущен.")
    while True:
        try:
            chat_id = await task_queue.get()
            logging.info(f"[Воркер-{worker_id}] Взял в работу пользователя {chat_id}.")
            user_data = user_sessions.get(chat_id)
            if not user_data or user_data.get('status') != 'ready':
                logging.warning(f"[Воркер-{worker_id}] Данные для {chat_id} не найдены, задача пропущена.")
                task_queue.task_done()
                continue
            
            max_restarts = 5
            for attempt in range(max_restarts):
                try:
                    await run_booking_session(app.bot, chat_id, user_data, worker_id, headless_mode)
                    # Если функция завершилась без исключений, значит бронирование успешно
                    logging.info(f"[Воркер-{worker_id}] Задача для {chat_id} успешно выполнена.")
                    break 
                except SessionResetException as e:
                    logging.warning(f"[Воркер-{worker_id}] Перезапуск сессии для {chat_id} (попытка {attempt + 1}/{max_restarts}). Причина: {e}")
                    if attempt + 1 >= max_restarts:
                        logging.error(f"Достигнут лимит перезапусков для {chat_id}. Задача остановлена.")
                        await app.bot.send_message(chat_id, "Достигнут лимит автоматических перезапусков. Задача остановлена.")
                        break
                    await asyncio.sleep(15)

            logging.info(f"[Воркер-{worker_id}] Завершил работу с пользователем {chat_id}.")
            if chat_id in user_sessions:
                del user_sessions[chat_id]
            task_queue.task_done()
        except Exception as e:
            logging.critical(f"Критическая ошибка в воркере {worker_id}: {e}")

async def update_lists_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logging.info(f"Пользователь {get_user_info(update)} выполнил команду /update_lists")
    await update.message.reply_text(
        "**Обновление справочников**\n\n"
        "Этот процесс запустит браузер. Вам нужно будет вручную авторизоваться на сайте e-consul.\n"
        "После успешного входа, не закрывайте браузер. Скрипт автоматически соберет данные.\n\n"
        "Нажмите 'Начать', чтобы продолжить.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Начать", callback_data="start_scraping")]])
    )
    return 10 # UPDATING_LISTS






async def start_scraping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запускает Playwright для сбора данных с максимальной маскировкой и паузами."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    bot = context.bot
    
    user_data_dir = os.path.join(os.getcwd(), f'browser_profile_{chat_id}')
    await query.edit_message_text(f"Запускаю браузер с постоянным профилем...")

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
        logging.info("Маскировка navigator.webdriver применена.")
        
        await page.goto("https://e-consul.gov.ua/", timeout=60000, wait_until="domcontentloaded")
        await bot.send_message(chat_id, 
            "Браузер запущен. Пожалуйста, войдите в свой аккаунт на сайте e-consul.\n"
            "У вас есть 5 минут. После успешного входа, вернитесь сюда.")

        await page.wait_for_url("**/messages**", timeout=300000)
        await bot.send_message(chat_id, "✅ Авторизация пройдена. Начинаю сбор данных...")
        
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
        
        await bot.send_message(chat_id, "Собираю список стран...")
        country_input = page.locator('input[id="country"]')
        await country_input.click()
        
        # --- ИЗМЕНЕНИЕ: Ждем не контейнер, а первый элемент в списке ---
        await expect(page.locator('ul[role="listbox"] li[role="option"]').first).to_be_visible(timeout=30000)
        
        country_list_raw = await page.locator('ul[role="listbox"] li[role="option"]').all_text_contents()
        country_list = sorted([c.strip() for c in country_list_raw if c.strip()])

        scraped_data = {'countries': country_list, 'institutions': {}}

        await bot.send_message(chat_id, f"Найдено {len(country_list)} стран. Теперь собираю учреждения...")
        
        for i, country in enumerate(country_list):
            try:
                await country_input.fill(country, delay=random.randint(50, 150))
                await page.wait_for_timeout(random.randint(800, 1500))
                await page.locator(f'li[role="option"]:has-text("{country}")').click()
                await page.wait_for_timeout(random.randint(2000, 3500))

                try:
                    await page.locator('input[id="consularInstitution"]').click()
                    # И здесь тоже ждем первый элемент, а не контейнер
                    await expect(page.locator('ul[role="listbox"] li[role="option"]').first).to_be_visible(timeout=20000)
                    institution_list_raw = await page.locator('ul[role="listbox"] li[role="option"]').all_text_contents()
                    scraped_data['institutions'][country] = [inst.strip() for inst in institution_list_raw if inst.strip()]
                    logging.info(f"Собраны учреждения для страны: {country}")
                except PlaywrightTimeoutError:
                    logging.warning(f"Для страны '{country}' не удалось загрузить список учреждений (таймаут). Вероятно, он пуст.")
                    scraped_data['institutions'][country] = []

                await country_input.clear()
                
                pause_duration = random.randint(5, 15)
                logging.info(f"Обработано {i + 1}/{len(country_list)}: {country}. Пауза {pause_duration} секунд...")
                await page.wait_for_timeout(pause_duration * 1000)

                if (i + 1) % 5 == 0:
                    await bot.send_message(chat_id, f"Обработано {i + 1}/{len(country_list)} стран...")
            except Exception as e:
                logging.error(f"Не удалось обработать страну '{country}': {e}. Пропускаю.")
                await page.reload()
                await page.wait_for_url("**/placeOfVisitInfo**", timeout=60000)
                country_input = page.locator('input[id="country"]')

        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(scraped_data, f, ensure_ascii=False, indent=4)

        await bot.send_message(chat_id, f"🎉 **Готово!**\nСправочники успешно обновлены. Найдено {len(country_list)} стран.")

    except PlaywrightTimeoutError as e:
        logging.error(f"Произошел таймаут на одном из шагов: {e}")
        await bot.send_message(chat_id, "❗️ **Ошибка:** Таймаут на одном из шагов. Возможно, сайт заблокировал вас. Попробуйте сменить IP (VPN).")
    except Exception as e:
        logging.error(f"Произошла критическая ошибка в start_scraping: {e}", exc_info=True)
        await bot.send_message(chat_id, f"❗️ Произошла критическая ошибка: {e}")
    finally:
        if browser_context: await browser_context.close()
        if p: await p.stop()
    return ConversationHandler.END




# --- Telegram Handlers ---
(
    SELECTING_AUTH, AWAITING_KEY_FILE, AWAITING_PASSWORD,
    AWAITING_DOB_GENDER, AWAITING_DOB_CALENDAR,
    AWAITING_COUNTRY, AWAITING_INSTITUTION,
    AWAITING_SERVICE, AWAITING_RECIPIENT, AWAITING_CHILD_DATA
) = range(10)

UPDATING_LISTS = 10

def get_user_info(update: Update) -> str:
    user = update.effective_user
    return f"user_id: {user.id}, name: '{user.first_name}', username: @{user.username}"

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.info(f"Пользователь {get_user_info(update)} выполнил команду /start")
    await update.message.reply_text("👋 **Привет!**\n\nЯ бот для записи на консульские услуги.")

async def book_new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logging.info(f"Пользователь {get_user_info(update)} выполнил команду /book_new")
    chat_id = update.effective_chat.id
    if chat_id in list(task_queue._queue) or (user_sessions.get(chat_id) and user_sessions[chat_id].get('status') == 'ready'):
        await update.message.reply_text("❗️ Ваша задача уже в очереди. Для отмены используйте /cancel.")
        return ConversationHandler.END
    user_sessions[chat_id] = {'chat_id': chat_id}
    keyboard = [
        [InlineKeyboardButton("Дія.Підпис", callback_data='diia')],
        [InlineKeyboardButton("Особистий ключ (файл)", callback_data='key')],
        [InlineKeyboardButton("ID.GOV.UA (BankID)", callback_data='idgov')],
    ]
    await update.message.reply_text(
        "**Шаг 1/5: Метод входа**\n\nВыберите метод аутентификации:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECTING_AUTH

async def book_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logging.info(f"Пользователь {get_user_info(update)} выполнил команду /book")
    chat_id = update.effective_chat.id
    if chat_id in list(task_queue._queue) or (user_sessions.get(chat_id) and user_sessions[chat_id].get('status') == 'ready'):
        await update.message.reply_text("❗️ Ваша задача уже в очереди. Для отмены используйте /cancel.")
        return ConversationHandler.END
    persisted_data = load_user_data(chat_id)
    if not persisted_data:
        await update.message.reply_text("Сохраненные данные не найдены. Начинаю новую настройку...")
        return await book_new_command(update, context)
    
    user_sessions[chat_id] = persisted_data
    user_sessions[chat_id]['status'] = 'ready'
    await task_queue.put(chat_id)
    logging.info(f"Пользователь {chat_id} добавлен в очередь с сохраненными данными.")
    await update.message.reply_text("✅ **Использую сохраненные данные.**\nЗадача добавлена в очередь.")
    return ConversationHandler.END

async def select_auth_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    auth_method = query.data
    user_sessions[chat_id]['auth_method'] = auth_method
    await query.edit_message_text(text=f"Выбран метод: **{auth_method.upper()}**.")
    if auth_method == 'key':
        await context.bot.send_message(chat_id, "Прикрепите ваш файл ключа (`.dat`, `.pfx`, и т.д.).")
        return AWAITING_KEY_FILE
    await context.bot.send_message(chat_id, "**Шаг 2/5: Данные заявителя**\n\nОберіть вашу стать:",
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
    await update.message.reply_text("✅ Файл получен. Теперь введите пароль от этого ключа.")
    return AWAITING_PASSWORD

async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    user_sessions[chat_id]['key_password'] = update.message.text
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        await update.message.reply_text("Пароль получен и удален.")
    except: pass
    await context.bot.send_message(chat_id, "**Шаг 2/5: Данные заявителя**\n\nОберіть вашу стать:",
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
    await query.edit_message_text("Стать выбрана. Теперь выберите дату рождения:", 
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
        await query.edit_message_text(f"Выбрана дата: {selected_date_str}")
        
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                all_countries = data['countries']
        except (FileNotFoundError, KeyError):
            await context.bot.send_message(chat_id, f"❗️ Файл с данными `{DATA_FILE}` не найден. Запустите /update_lists.")
            return ConversationHandler.END

        main_countries_def = ['Ірландія', 'Польща', 'Німеччина', 'США', 'Чехія', 'Іспанія', 'Італія', 'Канада']
        main_countries = [c for c in main_countries_def if c in all_countries]
        other_countries = sorted([c for c in all_countries if c not in main_countries])

        context.chat_data['main_countries'] = main_countries
        context.chat_data['other_countries'] = other_countries
        
        keyboard = []
        for country in main_countries:
            keyboard.append([InlineKeyboardButton(country, callback_data=f"country_{country}")])
        if other_countries:
            keyboard.append([InlineKeyboardButton("Другие страны >>", callback_data="show_other_countries")])
        
        await context.bot.send_message(chat_id, "**Шаг 3/5: Местоположение**\n\nВыберите страну:", 
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
        await query.edit_message_reply_markup(
            reply_markup=get_paginated_menu(context.chat_data['other_countries'], 0, "other_countries", "show_main_countries")
        )
        return AWAITING_COUNTRY
    
    if query.data in ["other_countries_prev", "other_countries_next"]:
        page = context.chat_data.get('other_countries_page', 0)
        page = max(0, page - 1) if query.data.endswith("_prev") else page + 1
        context.chat_data['other_countries_page'] = page
        await query.edit_message_reply_markup(
            reply_markup=get_paginated_menu(context.chat_data['other_countries'], page, "other_countries", "show_main_countries")
        )
        return AWAITING_COUNTRY

    if query.data == "show_main_countries":
        main_countries_menu_keyboard = []
        for country in context.chat_data['main_countries']:
            main_countries_menu_keyboard.append([InlineKeyboardButton(country, callback_data=f"country_{country}")])
        if context.chat_data['other_countries']:
            main_countries_menu_keyboard.append([InlineKeyboardButton("Другие страны >>", callback_data="show_other_countries")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(main_countries_menu_keyboard))
        return AWAITING_COUNTRY
        
    country_name = query.data.split('_', 1)[1]
    user_sessions[chat_id]['country'] = country_name
    
    with open(DATA_FILE, 'r', encoding='utf-8') as f: data = json.load(f)
    institutions_list = data['institutions'].get(country_name, [])

    keyboard = [[InlineKeyboardButton(text=(name[:60] + '..') if len(name) > 62 else name, callback_data=f"inst_{i}")] for i, name in enumerate(institutions_list)]
    if not keyboard:
        await query.edit_message_text(f"Для страны {country_name} не найдены учреждения.")
        return ConversationHandler.END

    await query.edit_message_text(f"Страна: {country_name}. Выберите учреждение:", 
                                  reply_markup=InlineKeyboardMarkup(keyboard))
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
    
    keyboard = [[InlineKeyboardButton("Оформлення закордонного паспорта", callback_data="service_passport")]] # Упрощенный пример
    
    await query.edit_message_text(f"Учреждение: {inst_name}.\n\n**Шаг 4/5: Услуга**\n\nВыберите тип услуги:",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAITING_SERVICE

async def select_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    
    service_name = "Оформлення закордонного паспорта"
    user_sessions[chat_id]['service_type'] = service_name
    
    keyboard = [
        [InlineKeyboardButton("Для себе", callback_data="recipient_self")],
        [InlineKeyboardButton("Для своєї дитини/підопічного", callback_data="recipient_child")]
    ]
    await query.edit_message_text(f"Услуга: {service_name}.\n\n**Шаг 5/5: Получатель**\n\nДля кого услуга?",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAITING_RECIPIENT

async def select_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    recipient = query.data.split('_')[1]
    user_sessions[chat_id]['service_recipient'] = recipient
    if recipient == 'child':
        await query.edit_message_text("Введите ФИО ребенка в формате: Фамилия Имя Отчество")
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
        await update.message.reply_text("Неверный формат. Введите Фамилию и Имя.")
        return AWAITING_CHILD_DATA

async def finalize_setup(message, chat_id, context):
    await save_user_data(chat_id, user_sessions[chat_id])
    user_sessions[chat_id]['status'] = 'ready'
    await message.reply_text("🎉 **Настройка завершена!**\nЗадача добавлена в очередь.")
    await task_queue.put(chat_id)
    logging.info(f"Пользователь {chat_id} добавлен в очередь.")
    return ConversationHandler.END

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    queue_size = task_queue.qsize()
    position_text = "Вашей задачи нет в очереди."
    try:
        user_position = list(task_queue._queue).index(update.effective_chat.id) + 1
        position_text = f"Ваша позиция в очереди: **{user_position}**."
    except ValueError: pass
    await update.message.reply_text(f"⚙️ Воркеров: **{WORKER_COUNT}** | 👥 В очереди: **{queue_size}**\n{position_text}")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    if chat_id in user_sessions: del user_sessions[chat_id]
    
    # Correctly remove from asyncio.Queue
    temp_queue = []
    while not task_queue.empty():
        item = await task_queue.get()
        if item != chat_id:
            temp_queue.append(item)
    for item in temp_queue:
        await task_queue.put(item)

    await update.message.reply_text("Все ваши задачи отменены.")
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(f"Ошибка в telegram-handler: {context.error}", exc_info=context.error)

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)s - %(message)s',
        handlers=[logging.FileHandler("bot_activity.log", encoding='utf-8'), logging.StreamHandler(sys.stdout)]
    )
    
    parser = argparse.ArgumentParser(description="Бот для бронирования слотов.")
    parser.add_argument('--headless', action='store_true', help="Запуск в скрытом режиме.")
    args = parser.parse_args()
    
    from functools import partial
    
    async def post_init(application: Application):
        os.makedirs(BASE_SESSIONS_DIR, exist_ok=True)
        os.makedirs(BASE_SCREENSHOTS_DIR, exist_ok=True)
        os.makedirs(BASE_KEYS_DIR, exist_ok=True)
        os.makedirs(USER_DATA_ARCHIVES_DIR, exist_ok=True)
        commands = [
            BotCommand("book", "▶️ Быстрый запуск (с вашими данными)"),
            BotCommand("book_new", "📝 Новая настройка (или обновить данные)"),
            BotCommand("status", "📊 Узнать статус"),
            BotCommand("cancel", "❌ Отменить задачу"),
            BotCommand("update_lists", "🔄 Обновить списки стран (админ)")
        ]
        await application.bot.set_my_commands(commands)
        logging.info("Команды меню успешно установлены.")
        for i in range(WORKER_COUNT):
            asyncio.create_task(worker(worker_id=i + 1, app=application, headless_mode=args.headless))
            
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("book_new", book_new_command), CommandHandler("book", book_command)],
        states={
            SELECTING_AUTH: [CallbackQueryHandler(select_auth_method, pattern='^(diia|key|idgov)$')],
            AWAITING_KEY_FILE: [MessageHandler(filters.Document.ALL, receive_key_file)],
            AWAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
            AWAITING_DOB_GENDER: [CallbackQueryHandler(select_gender, pattern='^gender_')],
            AWAITING_DOB_CALENDAR: [CallbackQueryHandler(calendar_handler, pattern='^calendar_')],
            AWAITING_COUNTRY: [CallbackQueryHandler(select_country, pattern='^(country_|show_|other_countries_)')],
            AWAITING_INSTITUTION: [CallbackQueryHandler(select_institution, pattern='^inst_')],
            AWAITING_SERVICE: [CallbackQueryHandler(select_service, pattern='^service_')],
            AWAITING_RECIPIENT: [CallbackQueryHandler(select_recipient, pattern='^recipient_')],
            AWAITING_CHILD_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_child_data)],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        per_user=False, per_chat=True,
    )

    update_lists_handler = ConversationHandler(
        entry_points=[CommandHandler("update_lists", update_lists_command)],
        states={
            UPDATING_LISTS: [CallbackQueryHandler(start_scraping, pattern='^start_scraping$')]
        },
        fallbacks=[CommandHandler("cancel", cancel_command)]
    )
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(conv_handler)
    application.add_handler(update_lists_handler)
    application.add_error_handler(error_handler)
    
    if not OWNER_TELEGRAM_ID:
        logging.warning("ПРЕДУПРЕЖДЕНИЕ: OWNER_TELEGRAM_ID не задан.")

    logging.info(f"Бот запускается с {WORKER_COUNT} воркерами...")
    mode = "скрытом (headless)" if args.headless else "видимом"
    logging.info(f"Браузеры будут запускаться в {mode} режиме.")
    application.run_polling()

if __name__ == "__main__":
    main()