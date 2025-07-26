import asyncio
import os
import random
from datetime import datetime
import httpx # Импортируем httpx для перехвата ошибок соединения
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Keyboard
# ОБНОВЛЕННЫЙ ИМПОРТ: Добавлены классы для интерактивных кнопок
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import TelegramError
import sys
import argparse
import re
import json # Добавлен импорт для работы с JSON
import calendar # Импорт для генерации календаря
import shutil # ИЗМЕНЕНИЕ: Добавлен для копирования файлов
# ИЗМЕНЕНИЕ: Добавлен импорт для работы с .env файлом
from dotenv import load_dotenv

# ИЗМЕНЕНИЕ: Загружаем переменные окружения из файла .env
load_dotenv()


# --- Класс для специального исключения при сбросе сессии ---
class SessionResetException(Exception):
    """Исключение, вызываемое при сбросе сессии на сайте."""
    pass

# --- Глобальная переменная для текущего ID запроса ---
CURRENT_REQUEST_ID = None

# --- Вспомогательная функция для логирования с меткой времени ---
original_print = print

def print_timestamped(message):
    """Выводит сообщение в консоль с текущей датой и временем, ID процесса и ID запроса."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    process_info = f" [Процесс: {CONFIG.get('process_id', 'N/A')}]"
    request_info = ""
    if CURRENT_REQUEST_ID:
        # Сокращаем ID запроса для компактности логов
        request_info = f" [Запрос ID: {CURRENT_REQUEST_ID[:8]}...] "

    original_print(f"[{timestamp}]{process_info}{request_info} {message}")

# --- КОНФИГУРАЦИЯ ---
CONFIG = {
    'base_url': 'https://e-consul.gov.ua',
    'timeout': 170000,
    'user_agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    'retry_count': 1,
    # ИЗМЕНЕНИЕ: Токен и ID чата теперь загружаются из файла .env
    'telegram_bot_token': os.getenv('TELEGRAM_BOT_TOKEN'),
    'telegram_chat_id': os.getenv('TELEGRAM_CHAT_ID'),
    'max_reload_attempts': 999999999,
    'reload_delay_seconds': 5,
    'post_search_delay_seconds': 3,
    'network_error_pause_seconds': 30,
    # Новые базовые папки для всех сессий и скриншотов
    'base_sessions_dir': 'sessions_data',
    'base_screenshots_dir': 'screenshots_data',
    # ИЗМЕНЕНИЕ: Папка для активных ключей теперь создается локально, а не в OneDrive.
    'base_keys_dir': 'active_keys_data',


    # Используем заполнитель {id} для уникальности папки и файла внутри базовых папок
    'screenshots_dir': '', # Будет сформирован динамически
    'storage_state_path': '', # Будет сформирован динамически
    'process_id': None, # Будет установлено из аргументов командной строки
}

# ИЗМЕНЕНИЕ: Глобальный словарь для хранения данных, введенных пользователем.
# Теперь он может быть предзаполнен из файла.
USER_DATA = {}

# Глобальная переменная для управления подробными Telegram-уведомлениями
VERBOSE_TELEGRAM_NOTIFICATIONS = False

# Инициализация Telegram бота
bot = Bot(token=CONFIG['telegram_bot_token'])

async def load_last_user_data():
    """
    НОВАЯ ФУНКЦИЯ: Загружает данные из последнего архива.
    Возвращает True, если данные успешно загружены, иначе False.
    """
    global USER_DATA
    base_archive_dir = 'user_data_archives'
    if not os.path.exists(base_archive_dir):
        print_timestamped("Папка с архивами данных не найдена. Будут запрошены новые данные.")
        return False

    try:
        all_archives = [d for d in os.listdir(base_archive_dir) if os.path.isdir(os.path.join(base_archive_dir, d))]
        if not all_archives:
            print_timestamped("Архивы данных не найдены. Будут запрошены новые данные.")
            return False

        # Сортируем по имени, что теперь соответствует сортировке по времени благодаря формату "ГГГГММДД_ЧЧММСС_process_ID"
        latest_archive = sorted(all_archives)[-1]
        latest_archive_path = os.path.join(base_archive_dir, latest_archive)
        data_file_path = os.path.join(latest_archive_path, 'user_input_data.json')

        if not os.path.exists(data_file_path):
            print_timestamped(f"В последнем архиве '{latest_archive}' не найден файл данных. Будут запрошены новые данные.")
            return False

        with open(data_file_path, 'r', encoding='utf-8') as f:
            USER_DATA = json.load(f)

        # ИЗМЕНЕНИЕ: Восстанавливаем файл ключа из архива, если он есть
        key_file_name = USER_DATA.get('key_file_name')
        if key_file_name:
            archived_key_path = os.path.join(latest_archive_path, key_file_name)
            if os.path.exists(archived_key_path):
                # Создаем папку для ключей текущего процесса
                current_process_key_dir = os.path.join(CONFIG['base_keys_dir'], CONFIG['process_id'])
                os.makedirs(current_process_key_dir, exist_ok=True)

                # Копируем ключ из архива в рабочую папку
                active_key_path = os.path.join(current_process_key_dir, key_file_name)
                shutil.copy(archived_key_path, active_key_path)

                # Обновляем путь к ключу в USER_DATA, чтобы он указывал на активный файл
                USER_DATA['key_file_path'] = active_key_path
                print_timestamped(f"Файл ключа '{key_file_name}' восстановлен из архива в '{active_key_path}'")
            else:
                print_timestamped(f"Предупреждение: Файл ключа '{key_file_name}' указан в данных, но не найден в архиве '{latest_archive_path}'. Ключ будет запрошен заново.")
                # Очищаем данные, связанные с ключом, чтобы запросить их снова
                USER_DATA.pop('key_file_path', None)
                USER_DATA.pop('key_file_name', None)
                USER_DATA.pop('key_password', None)
                USER_DATA.pop('knedp_choice', None)


        print_timestamped(f"Успешно загружены данные из последнего запуска: {latest_archive}")
        await send_telegram_message(f"Используются данные из предыдущего запуска. Для ввода новых данных перезапустите скрипт с аргументом --new.", is_verbose=False)
        return True
    except Exception as e:
        print_timestamped(f"Ошибка при загрузке последних данных: {e}. Будут запрошены новые данные.")
        await send_telegram_message(f"Ошибка при загрузке сохраненных данных: {e}. Данные будут запрошены заново.", is_verbose=False)
        USER_DATA = {} # Очищаем данные в случае ошибки
        return False

async def save_user_data_archive():
    """
    Сохраняет все введенные пользователем данные (включая файл ключа)
    в уникальную папку архива.
    """
    global USER_DATA
    if not USER_DATA:
        print_timestamped("Нет данных пользователя для архивации.")
        return

    try:
        # Создаем базовую папку для архивов, если ее нет
        base_archive_dir = 'user_data_archives'
        os.makedirs(base_archive_dir, exist_ok=True)

        # ИСПРАВЛЕНО: Создаем уникальную папку для этого запуска с меткой времени в начале для правильной сортировки
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_dir = os.path.join(base_archive_dir, f"{timestamp}_process_{CONFIG['process_id']}")
        os.makedirs(archive_dir)
        print_timestamped(f"Создана папка для архива данных: {archive_dir}")

        # Сохраняем JSON с данными
        data_file_path = os.path.join(archive_dir, 'user_input_data.json')
        with open(data_file_path, 'w', encoding='utf-8') as f:
            json.dump(USER_DATA, f, ensure_ascii=False, indent=4)
        print_timestamped(f"Данные пользователя сохранены в: {data_file_path}")

        # Копируем файл ключа, если он был использован
        key_path = USER_DATA.get('key_file_path')
        if key_path and os.path.exists(key_path):
            shutil.copy(key_path, archive_dir)
            print_timestamped(f"Файл ключа скопирован в архив: {os.path.join(archive_dir, os.path.basename(key_path))}")

        await send_telegram_message(f"Все введенные данные сохранены в архив на сервере.", is_verbose=True)

    except Exception as e:
        error_message = f"Не удалось заархивировать данные пользователя: {e}"
        print_timestamped(error_message)
        await send_telegram_message(error_message, is_verbose=False)


async def generate_year_selection_keyboard(base_year: int, original_month: int) -> InlineKeyboardMarkup:
    """
    Генерирует навигационную клавиатуру для выбора года.
    """
    keyboard = []
    # Заголовок
    header = [InlineKeyboardButton("Выберите год", callback_data="calendar_ignore")]
    keyboard.append(header)

    # Создаем сетку годов 4x3, центрированную вокруг базового года
    start_year = base_year - 6
    year_buttons_row = []
    for year in range(start_year, start_year + 12):
        # При выборе года, мы переходим на этот год, сохраняя исходный месяц
        year_buttons_row.append(InlineKeyboardButton(str(year), callback_data=f"calendar_nav_{year}_{original_month}"))
        if len(year_buttons_row) == 4:
            keyboard.append(year_buttons_row)
            year_buttons_row = []
    if year_buttons_row:
        keyboard.append(year_buttons_row)

    # Навигация по блокам годов и кнопка "Назад"
    nav_row = [
        InlineKeyboardButton("<<", callback_data=f"calendar_year_nav_{base_year - 12}_{original_month}"),
        InlineKeyboardButton("Назад", callback_data=f"calendar_nav_{base_year}_{original_month}"),
        InlineKeyboardButton(">>", callback_data=f"calendar_year_nav_{base_year + 12}_{original_month}")
    ]
    keyboard.append(nav_row)

    return InlineKeyboardMarkup(keyboard)

async def generate_month_selection_keyboard(year: int, original_month: int) -> InlineKeyboardMarkup:
    """
    Генерирует навигационную клавиатуру для выбора месяца.
    """
    month_names = [
        "Янв", "Фев", "Мар", "Апр", "Май", "Июн",
        "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"
    ]
    keyboard = []
    # Заголовок
    header = [InlineKeyboardButton(f"Выберите месяц для {year} года", callback_data="calendar_ignore")]
    keyboard.append(header)

    # Создаем сетку месяцев 4x3
    month_buttons_row = []
    for i, month_name in enumerate(month_names):
        month_number = i + 1
        month_buttons_row.append(InlineKeyboardButton(month_name, callback_data=f"calendar_nav_{year}_{month_number}"))
        if len(month_buttons_row) == 4:
            keyboard.append(month_buttons_row)
            month_buttons_row = []

    # Кнопка "Назад"
    back_button_row = [InlineKeyboardButton("Назад", callback_data=f"calendar_nav_{year}_{original_month}")]
    keyboard.append(back_button_row)

    return InlineKeyboardMarkup(keyboard)

async def generate_calendar_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    """
    Генерирует InlineKeyboardMarkup с календарем для указанного года и месяца.
    Кнопки года и месяца теперь вызывают соответствующие режимы выбора.
    """
    # Русские названия месяцев для заголовка
    month_names = [
        "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
        "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
    ]

    cal = calendar.monthcalendar(year, month)
    keyboard = []

    # 1-й ряд: Название месяца и кнопка года
    header_buttons = [
        InlineKeyboardButton(f"{month_names[month]}", callback_data=f"calendar_show_months_{year}_{month}"),
        InlineKeyboardButton(f"{year}", callback_data=f"calendar_show_years_{year}_{month}")
    ]
    keyboard.append(header_buttons)

    # 2-й ряд: Дни недели
    days_of_week_buttons = [InlineKeyboardButton(day, callback_data="calendar_ignore") for day in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]]
    keyboard.append(days_of_week_buttons)

    # Ряды с датами
    for week in cal:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="calendar_ignore"))
            else:
                callback = f"calendar_select_{year}_{month}_{day}"
                row.append(InlineKeyboardButton(str(day), callback_data=callback))
        keyboard.append(row)

    # Логика для предыдущего и следующего месяцев
    prev_month, prev_year = (month - 1, year) if month > 1 else (12, year - 1)
    next_month, next_year = (month + 1, year) if month < 12 else (1, year + 1)

    nav_buttons = [
        InlineKeyboardButton("<", callback_data=f"calendar_nav_{prev_year}_{prev_month}"),
        InlineKeyboardButton("Сегодня", callback_data=f"calendar_nav_{datetime.now().year}_{datetime.now().month}"),
        InlineKeyboardButton(">", callback_data=f"calendar_nav_{next_year}_{next_month}")
    ]
    keyboard.append(nav_buttons)

    return InlineKeyboardMarkup(keyboard)


async def send_telegram_message(message, is_verbose=False):
    """
    Отправляет текстовое сообщение в Telegram.
    Если is_verbose=True, сообщение отправляется только если VERBOSE_TELEGRAM_NOTIFICATIONS включен.
    Включает ID процесса и ID запроса в сообщение.
    """
    if not CONFIG['telegram_bot_token'] or not CONFIG['telegram_chat_id']:
        return

    # Если сообщение подробное и verbose-режим не включен, не отправляем
    if is_verbose and not VERBOSE_TELEGRAM_NOTIFICATIONS:
        return

    max_message_length = 4096

    # Добавляем ID процесса и ID запроса к каждому сообщению
    process_info = f"Процесс {CONFIG.get('process_id', 'Неизвестно')}"
    request_info_tg = ""
    if CURRENT_REQUEST_ID:
        request_info_tg = f", Запрос ID {CURRENT_REQUEST_ID[:8]}..."

    full_message = f"{process_info}{request_info_tg}: {message}"

    if len(full_message) > max_message_length:
        full_message = full_message[:max_message_length - 3] + "..."

    try:
        await bot.send_message(chat_id=CONFIG['telegram_chat_id'], text=full_message)
    except (TelegramError, httpx.ConnectError) as e:
        print_timestamped(f"Предупреждение: Не удалось отправить текстовое сообщение в Telegram из-за ошибки: {e}")
    except Exception as e:
        print_timestamped(f"Неизвестная ошибка при отправке текстового сообщения в Telegram: {e}")

async def save_screenshot(page, name, locator=None, full_page=False, timeout=30000):
    """
    Сохраняет скриншот страницы или конкретного элемента с уникальным именем,
    основанным на метке времени, в указанную папку для скриншотов.
    Возвращает путь к сохраненному файлу.

    Добавлен параметр 'locator' для скриншота конкретного элемента.
    По умолчанию full_page=False, если передан locator.
    """
    # Создаем папку для скриншотов, если она не существует
    if not os.path.exists(CONFIG['screenshots_dir']):
        os.makedirs(CONFIG['screenshots_dir'])
        print_timestamped(f"Создана папка для скриншотов: {CONFIG['screenshots_dir']}")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Формируем полный путь к файлу скриншота
    path = os.path.join(CONFIG['screenshots_dir'], f"{name}_{timestamp}.png")
    try:
        if page.is_closed():
            print_timestamped(f"Не удалось сохранить скриншот '{name}': страница уже закрыта.")
            return None

        if locator:
            await locator.screenshot(path=path, timeout=timeout) # Скриншот элемента
            print_timestamped(f"Скриншот элемента сохранен: {path}")
        else:
            await page.screenshot(path=path, full_page=full_page, timeout=timeout) # Скриншот всей страницы
            print_timestamped(f"Скриншот страницы сохранен: {path}")

        return path
    except Exception as e:
        print_timestamped(f"Ошибка при сохранении скриншота '{name}': {e}")
        return None

async def send_telegram_photo(photo_path, caption="Скриншот"):
    """
    Отправляет скриншот в указанный Telegram чат.
    Добавлена обработка ошибок отправки.
    Включает ID процесса и ID запроса в подпись.
    После успешной отправки, скриншот удаляется с диска.
    """
    if not CONFIG['telegram_bot_token'] or not CONFIG['telegram_chat_id']:
        print_timestamped("Telegram BOT_TOKEN или CHAT_ID не настроены. Скриншот не будет отправлен.")
        return

    if photo_path is None or not os.path.exists(photo_path):
        print_timestamped(f"Невозможно отправить: файл скриншота не найден по пути '{photo_path}'")
        return

    max_caption_length = 1024

    # Добавляем ID процесса и ID запроса к подписи
    process_info = f"Процесс {CONFIG.get('process_id', 'Неизвестно')}"
    request_info_tg = ""
    if CURRENT_REQUEST_ID:
        request_info_tg = f", Запрос ID {CURRENT_REQUEST_ID[:8]}..."

    full_caption = f"{process_info}{request_info_tg}: {caption}"

    if len(full_caption) > max_caption_length:
        full_caption = full_caption[:max_caption_length - 3] + "..."

    try:
        with open(photo_path, 'rb') as photo_file:
            await bot.send_photo(chat_id=CONFIG['telegram_chat_id'], photo=photo_file, caption=full_caption)
        print_timestamped(f"Скриншот '{photo_path}' успешно отправлен в Telegram.")
        # --- ДОБАВЛЕНО: Удаление скриншота после успешной отправки ---
        if os.path.exists(photo_path):
            os.remove(photo_path)
            print_timestamped(f"Скриншот '{photo_path}' удален с диска после отправки.")
        # --- КОНЕЦ ДОБАВЛЕНИЯ ---
    except (TelegramError, httpx.ConnectError) as e:
        print_timestamped(f"Предупреждение: Не удалось отправить скриншот '{photo_path}' в Telegram из-за ошибки: {e}")
    except Exception as e:
        print_timestamped(f"Неизвестная ошибка при отправке скриншота в Telegram: {e}")

async def manual_mode(page, last_error_info="Неизвестная ошибка", browser_instance=None):
    """
    Включает ручной режим управления браузером.
    Возвращает True, если пользователь ввел 'exit', иначе False.
    """
    original_print("\n" + "="*60)
    original_print(" ВНИМАНИЕ: РУЧНОЙ РЕЖИМ ".center(60, "="))
    original_print("="*60)
    print_timestamped(f"Причина: {last_error_info[:100]}...")
    print_timestamped("Браузер останется открытым для ручного управления. НЕ ЗАКРЫВАЙТЕ ОКНО БРАУЗЕРА ВРУЧНУЮ! Или напишите exit")
    print_timestamped("Доступные команды:")
    print_timestamped(" - 'capture' для скриншота")
    print_timestamped(" - 'select_locator' для попытки определить локатор кликнутого элемента")
    print_timestamped(" - 'exit' для выхода (закроет браузер)")
    original_print("="*60 + "\n")

    await send_telegram_message(f"Переход в ручной режим: {last_error_info[:200]}...", is_verbose=False) # Важное сообщение

    while True:
        try:
            cmd = await asyncio.to_thread(input, "Ваша команда: ")
            cmd = cmd.strip().lower()
            if cmd == 'capture':
                if page and page.is_closed():
                    print_timestamped("Страница была закрыта. Невозможно сделать скриншот.")
                    continue
                path = await save_screenshot(page, "manual_capture", timeout=CONFIG['timeout'])
                if path:
                    await send_telegram_photo(path, f"Скриншот из ручного режима.")
                    await send_telegram_message("Скриншот сделан и отправлен из ручного режима.", is_verbose=True)
            elif cmd == 'select_locator':
                if page and page.is_closed():
                    print_timestamped("Страница была закрыта. Невозможно выбрать локатор.")
                    continue
                print_timestamped("\nДля определения локатора вручную:")
                print_timestamped("1. В браузере, **кликните правой кнопкой мыши** по элементу, который нужно отладить.")
                print_timestamped("2. Выберите '**Проверить**' (или Inspect/Examine Element).")
                print_timestamped("3. Откроется панель инструментов разработчика.")
                print_timestamped("4. **В HTML-коде**, который выделился, кликните правой кнопкой мыши по выделенной строке.")
                print_timestamped("5. Выберите '**Копировать**' (Copy) -> '**Копировать селектор**' (Copy selector).")
                print_timestamped("6. Вставьте скопированный селектор сюда.")
                await send_telegram_message("Инструкции по определению локатора выведены в консоль.", is_verbose=True)

                selected_selector = await asyncio.to_thread(input, "Введите найденный селектор (или Enter для пропуска): ")
                if selected_selector:
                    print_timestamped(f"Вы можете попробовать использовать этот селектор: '{selected_selector}'")
                    print_timestamped("Замените им текущий локатор в соответствующем Шаге вашего скрипта.")
                    await send_telegram_message(f"Найден селектор: '{selected_selector}'.", is_verbose=True)
                else:
                    print_timestamped("Селектор не введен.")
                    await send_telegram_message("Селектор не введен.", is_verbose=True)

            elif cmd == 'exit':
                print_timestamped("Выход из ручного режима. Закрываю браузер...")
                await send_telegram_message("Выход из ручного режима. Закрытие браузера.", is_verbose=False)
                return True
            else:
                print_timestamped("Неизвестная команда. Введите 'capture', 'select_locator' или 'exit'.")
                await send_telegram_message("Неизвестная команда в ручном режиме.", is_verbose=True)
        except (EOFError, KeyboardInterrupt):
            print_timestamped("\nПрограмма прервана пользователем.")
            await send_telegram_message("Программа прервана пользователем в ручном режиме.", is_verbose=False)
            return True
        except Exception as e:
            print_timestamped(f"Ошибка в ручном режиме: {str(e)}")
            await send_telegram_message(f"Ошибка в ручном режиме: {str(e)}", is_verbose=False)
            return False

def extract_request_id_from_url(url):
    """Извлекает ID запроса из URL, если он соответствует паттерну /tasks/ID/."""
    match = re.search(r'/tasks/([a-f0-9-]+)/', url)
    if match:
        return match.group(1)
    return None

async def request_telegram_text(prompt_text, last_update_id, timeout_seconds=180):
    """
    Отправляет запрос в Telegram и ожидает текстовый ответ от пользователя.
    Возвращает (текст_ответа, новый_last_update_id) или (None, новый_last_update_id) при таймауте.
    """
    await bot.send_message(chat_id=CONFIG['telegram_chat_id'], text=prompt_text)
    print_timestamped(f"Запрос отправлен: '{prompt_text}'. Ожидание ответа ({timeout_seconds}с)...")

    response_text = None
    # <<< НАЧАЛО ИЗМЕНЕНИЙ: Усиленная защита от сбоев Telegram >>>
    for _ in range(timeout_seconds):
        try:
            updates = await bot.get_updates(offset=last_update_id, timeout=1)
            for update in updates:
                last_update_id = max(last_update_id, update.update_id + 1)
                if update.message and update.message.text:
                    response_text = update.message.text
                    try: 
                        await bot.delete_message(chat_id=update.message.chat.id, message_id=update.message.message_id)
                    except Exception:
                        pass 
                    break
            if response_text:
                break
        except TelegramError as e:
            if "Timed out" in str(e):
                print_timestamped("Предупреждение: Таймаут при связи с Telegram. Повторяю попытку...")
                await asyncio.sleep(2)
                continue
            else:
                print_timestamped(f"Критическая ошибка Telegram в request_telegram_text: {e}")
                raise e # Передаем дальше другие ошибки
        
        await asyncio.sleep(1)
    # <<< КОНЕЦ ИЗМЕНЕНИЙ >>>

    if not response_text:
        print_timestamped("Пользователь не предоставил текстовый ответ в течение таймаута.")
        await bot.send_message(
            chat_id=CONFIG['telegram_chat_id'],
            text=f"Час очікування на введення тексту '{prompt_text}' вичерпано."
        )
        return None, last_update_id

    print_timestamped(f"Получен ответ.") 
    return response_text, last_update_id


async def perform_login_steps(page):
    """
    Отправляет в Telegram запрос на выбор метода авторизации, ожидает ответа
    и затем выполняет соответствующие шаги для входа.
    """
    global USER_DATA

    auth_method = USER_DATA.get('auth_method')

    if auth_method:
        print_timestamped(f"Использую сохраненный метод авторизации: {auth_method.upper()}")
        await send_telegram_message(f"Используется сохраненный метод авторизации: {auth_method.upper()}", is_verbose=True)
    else:
        print_timestamped("Шаг 1: Запрос метода авторизации у пользователя в Telegram...")
        keyboard = [
            [InlineKeyboardButton("Дія.Підпис", callback_data='diia')],
            [InlineKeyboardButton("Особистий ключ", callback_data='key')],
            [InlineKeyboardButton("ID.GOV.UA (BankID)", callback_data='idgov')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            message = await bot.send_message(
                chat_id=CONFIG['telegram_chat_id'],
                text="Для авторизації виберіть один з доступних методів:",
                reply_markup=reply_markup
            )
            print_timestamped("Сообщение с выбором отправлено. Ожидание ответа пользователя (таймаут 3 минуты)...")
        except Exception as e:
            raise Exception(f"Не удалось отправить сообщение с выбором в Telegram: {e}")

        last_update_id = 0
        # <<< НАЧАЛО ИЗМЕНЕНИЙ: Усиленная защита от сбоев Telegram >>>
        for _ in range(180):
            try:
                updates = await bot.get_updates(offset=last_update_id, timeout=1)
                for update in updates:
                    last_update_id = max(last_update_id, update.update_id + 1)
                    if update.callback_query and update.callback_query.message.message_id == message.message_id:
                        query = update.callback_query
                        auth_method = query.data
                        await query.answer()
                        await bot.edit_message_text(
                            chat_id=query.message.chat.id,
                            message_id=query.message.message_id,
                            text=f"Вибрано: {auth_method.upper()}"
                        )
                        print_timestamped(f"Пользователь выбрал: {auth_method}")
                        break
                if auth_method:
                    break
            except TelegramError as e:
                if "Timed out" in str(e):
                    print_timestamped("Предупреждение: Таймаут при связи с Telegram. Повторяю попытку...")
                    await asyncio.sleep(2)
                    continue
                else:
                    print_timestamped(f"Критическая ошибка Telegram в perform_login_steps (выбор метода): {e}")
                    raise e
            
            await asyncio.sleep(1)
        # <<< КОНЕЦ ИЗМЕНЕНИЙ >>>

        if not auth_method:
            try:
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    text="Час очікування вичерпано. Перезапустіть процес."
                )
            except Exception as e:
                print_timestamped(f"Не удалось обновить сообщение о тайм-ауте в Telegram: {e}")
            raise Exception("Пользователь не выбрал метод авторизации в течение 3 минут.")
        
        USER_DATA['auth_method'] = auth_method

    if auth_method == 'diia':
        await send_telegram_message("Шаг 1: Выбран метод 'Дія.Підпис'. Начинаю авторизацию.", is_verbose=True)
        try:
            diia_button_selector = 'button:has-text("Дія.Підпис")'
            await page.locator(diia_button_selector).wait_for(state='visible', timeout=20000)
            print_timestamped("Нажимаю кнопку 'Дія.Підпис'...")
            await page.locator(diia_button_selector).click(timeout=30000)
            print_timestamped("Ожидаю загрузки QR-кода на текущей странице...")
            await page.wait_for_load_state('networkidle', timeout=CONFIG['timeout'])
            print_timestamped(f"Страница с QR-кодом 'Дія' загружена. URL: {page.url}")
            await send_telegram_message("Шаг 1.1 завершен: Переход на страницу с QR-кодом.", is_verbose=True)
            print_timestamped("Шаг 1.2: Поиск и захват QR-кода 'Дія'...")
            await page.wait_for_timeout(3000)
            qr_element = page.locator("canvas, div.qr-code, #qr-overlay").first
            await qr_element.wait_for(state='visible', timeout=60000)
            print_timestamped("Элемент QR-кода найден и видим.")
            await qr_element.scroll_into_view_if_needed()
            await page.wait_for_timeout(1000)
            qr_path = await save_screenshot(page, "diia_qr_code_capture", locator=qr_element, timeout=CONFIG['timeout'])
            if qr_path:
                await send_telegram_photo(qr_path, "Відскануйте QR-код у застосунку 'Дія' для авторизації.")
                await send_telegram_message("QR-код для 'Дія' отправлен. Ожидаю подтверждения в приложении...", is_verbose=False)
            else:
                error_msg = "Не удалось создать скриншот QR-кода для 'Дія.Підпис'."
                print_timestamped(error_msg)
                await send_telegram_message(error_msg, is_verbose=False)
                raise Exception(error_msg)
            print_timestamped("Ожидание завершения авторизации через 'Дія'...")
            await page.wait_for_url("**/e-consul.gov.ua/**", timeout=CONFIG['timeout'])
            await page.wait_for_load_state('networkidle', timeout=CONFIG['timeout'])
            print_timestamped(f"Авторизация через 'Дія.Підпис' успешно завершена. Текущий URL: {page.url}")
            await page.context.storage_state(path=CONFIG['storage_state_path'])
            print_timestamped(f"Состояние сессии сохранено после успешной авторизации в: {CONFIG['storage_state_path']}")
            await send_telegram_message("Шаг 1.3 завершен: Успешная авторизация и сохранение сессии.", is_verbose=False)
        except Exception as e:
            await send_telegram_message(f"Критическая ошибка на Шаге 1 ('Дія.Підпис'): {e}", is_verbose=False)
            raise Exception(f"Ошибка на Шаге 1 ('Дія.Підпис'): {e}")

    elif auth_method == 'idgov':
        await send_telegram_message("Шаг 1: Выбран метод ID.GOV.UA. Начинаю авторизацию.", is_verbose=True)
        try:
            id_gov_ua_button = page.locator('button').nth(2)
            await id_gov_ua_button.wait_for(state='visible', timeout=20000)
            await id_gov_ua_button.click(timeout=30000)
            await page.wait_for_load_state('networkidle', timeout=CONFIG['timeout'])
            print_timestamped(f"Страница загружена после клика. Текущий URL: {page.url}")
            await send_telegram_message("Шаг 1 завершен: Переход на ID.GOV.UA.", is_verbose=True)
        except Exception as e:
            raise Exception(f"Ошибка на Шаге 1 (ID.GOV.UA): {e}")

    elif auth_method == 'key':
        await send_telegram_message("Шаг 1: Выбран метод 'Особистий ключ'. Начинаю авторизацию.", is_verbose=True)
        last_update_id = 0
        saved_key_path = USER_DATA.get('key_file_path')
        saved_password = USER_DATA.get('key_password')
        saved_knedp = USER_DATA.get('knedp_choice')
        needs_user_input = not (saved_key_path and os.path.exists(saved_key_path) and saved_password and saved_knedp)

        if not needs_user_input:
            print_timestamped("Найдены сохраненные данные для авторизации по ключу. Попытка автоматического входа.")
            await send_telegram_message("Использую сохраненные данные ключа и пароль для входа.", is_verbose=False)
            try:
                personal_key_button = page.get_by_role("button", name="Особистий ключ")
                await personal_key_button.wait_for(state='visible', timeout=20000)
                await personal_key_button.click(timeout=30000)
                await page.wait_for_load_state('networkidle', timeout=CONFIG['timeout'])
                login_title_selector = 'h4#id-app-login-title:has-text("Увійдіть за допомогою особистого ключа")'
                await page.locator(login_title_selector).wait_for(state='visible', timeout=20000)
                async with page.expect_file_chooser() as fc_info:
                    await page.locator('text="Оберіть ключ на своєму носієві"').click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(saved_key_path)
                print_timestamped(f"Использую сохраненный файл ключа: {saved_key_path}")
                if saved_knedp == 'knedp_mono':
                    await page.locator('#id-app-login-sign-form-file-key-server').click(timeout=10000)
                    await page.locator('li[data-value="31"]').click(timeout=10000)
                    print_timestamped("Использую сохраненный КНЕДП: monobank")
                else:
                    raise NotImplementedError(f"Сохраненный КНЕДП '{saved_knedp}' не поддерживается для автоматического входа.")
                password_input_selector = 'input#id-app-login-sign-form-file-key-password'
                await page.locator(password_input_selector).fill(saved_password, timeout=10000)
                print_timestamped("Использую сохраненный пароль.")
                continue_button_selector = 'button#id-app-login-sign-form-file-key-sign-button'
                await page.locator(continue_button_selector).wait_for(state='visible', timeout=10000)
                async with page.expect_navigation(url="**/e-consul.gov.ua/**", timeout=CONFIG['timeout']):
                    await page.locator(continue_button_selector).click(timeout=20000)
                await page.wait_for_load_state('networkidle', timeout=CONFIG['timeout'])
                print_timestamped(f"Авторизация через сохраненный личный ключ успешно завершена. URL: {page.url}")
                await page.context.storage_state(path=CONFIG['storage_state_path'])
                print_timestamped(f"Состояние сессии сохранено после успешной авторизации в: {CONFIG['storage_state_path']}")
                await send_telegram_message("Успешная автоматическая авторизация через сохраненный 'Особистий ключ'.", is_verbose=False)
            except Exception as e:
                print_timestamped(f"Автоматический вход с сохраненными данными не удался: {e}. Запрашиваю данные у пользователя.")
                await send_telegram_message(f"Авто-вход по ключу не удался. Возможно, пароль изменился. Запрашиваю данные заново.", is_verbose=False)
                USER_DATA.pop('key_file_path', None)
                USER_DATA.pop('key_file_name', None)
                USER_DATA.pop('key_password', None)
                USER_DATA.pop('knedp_choice', None)
                needs_user_input = True
                print_timestamped("Перезагружаю страницу для повторного ввода данных...")
                await page.reload(timeout=CONFIG['timeout'])
                await page.wait_for_load_state('networkidle', timeout=CONFIG['timeout'])

        if needs_user_input:
            print_timestamped("Требуется ввод данных от пользователя для авторизации по ключу.")
            try:
                if not await page.locator('h4#id-app-login-title:has-text("Увійдіть за допомогою особистого ключа")').is_visible():
                    personal_key_button = page.get_by_role("button", name="Особистий ключ")
                    await personal_key_button.wait_for(state='visible', timeout=20000)
                    await personal_key_button.click(timeout=30000)
                    await page.wait_for_load_state('networkidle', timeout=CONFIG['timeout'])
                print_timestamped("Переход на страницу авторизации через 'Особистий ключ' выполнен.")
                login_title_selector = 'h4#id-app-login-title:has-text("Увійдіть за допомогою особистого ключа")'
                await page.locator(login_title_selector).wait_for(state='visible', timeout=20000)
                print_timestamped("Окно 'Увійдіть за допомогою особистого ключа' подтверждено.")
                await bot.send_message(chat_id=CONFIG['telegram_chat_id'], text="Прикріпіть файл з ключем.\nПідтримуються формати: .jks, .pfx, .pk8, .zs2, .dat")
                print_timestamped("Запрос на файл ключа отправлен в Telegram. Ожидание файла (таймаут 3 минуты)...")
                document = None
                
                # <<< НАЧАЛО ИЗМЕНЕНИЙ: Усиленная защита от сбоев Telegram >>>
                for _ in range(180):
                    try:
                        updates = await bot.get_updates(offset=last_update_id, timeout=1)
                        for update in updates:
                            last_update_id = max(last_update_id, update.update_id + 1)
                            if update.message and update.message.document:
                                document = update.message.document
                                break
                        if document:
                            break
                    except TelegramError as e:
                        if "Timed out" in str(e):
                            print_timestamped("Предупреждение: Таймаут при связи с Telegram. Повторяю попытку...")
                            await asyncio.sleep(2)
                            continue
                        else:
                            raise e
                    await asyncio.sleep(1)
                # <<< КОНЕЦ ИЗМЕНЕНИЙ >>>

                if not document:
                    raise Exception("Файл с ключем не был получен от пользователя в течение 3 минут.")
                
                file_id = document.file_id
                file_name = document.file_name
                tg_file = await bot.get_file(file_id)
                key_storage_dir = os.path.join(CONFIG['base_keys_dir'], CONFIG['process_id'])
                os.makedirs(key_storage_dir, exist_ok=True)
                key_file_path = os.path.join(key_storage_dir, file_name)
                await tg_file.download_to_drive(key_file_path)
                print_timestamped(f"Файл ключа '{file_name}' получен и сохранен в: {key_file_path}")
                await send_telegram_message(f"Файл '{file_name}' получен. Загружаю...", is_verbose=True)
                async with page.expect_file_chooser() as fc_info:
                    await page.locator('text="Оберіть ключ на своєму носієві"').click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(key_file_path)
                print_timestamped("Файл ключа успешно выбран в диалоге.")
                
                knedp_keyboard = [[InlineKeyboardButton('КНЕДП АЦСК АТ КБ "ПРИВАТБАНК"', callback_data='knedp_privat')], [InlineKeyboardButton("КНЕДП monobank | Universal Bank", callback_data='knedp_mono')]]
                knedp_reply_markup = InlineKeyboardMarkup(knedp_keyboard)
                knedp_message = await bot.send_message(chat_id=CONFIG['telegram_chat_id'], text="Виберіть Кваліфікованого надавача електронних довірчих послуг:", reply_markup=knedp_reply_markup)
                print_timestamped("Запрос на выбор КНЕДП отправлен. Ожидание выбора (таймаут 3 минуты)...")
                
                knedp_choice = None
                # <<< НАЧАЛО ИЗМЕНЕНИЙ: Усиленная защита от сбоев Telegram >>>
                for _ in range(180):
                    try:
                        updates = await bot.get_updates(offset=last_update_id, timeout=1)
                        for update in updates:
                            last_update_id = max(last_update_id, update.update_id + 1)
                            if update.callback_query and update.callback_query.message.message_id == knedp_message.message_id:
                                query = update.callback_query
                                knedp_choice = query.data
                                await query.answer()
                                await bot.edit_message_text(chat_id=query.message.chat.id, message_id=query.message.message_id, text=f"Вибрано КНЕДП: {query.data.split('_')[1].upper()}")
                                print_timestamped(f"Пользователь выбрал КНЕДП: {knedp_choice}")
                                break
                        if knedp_choice:
                            break
                    except TelegramError as e:
                        if "Timed out" in str(e):
                            print_timestamped("Предупреждение: Таймаут при связи с Telegram. Повторяю попытку...")
                            await asyncio.sleep(2)
                            continue
                        else:
                            raise e
                    await asyncio.sleep(1)
                # <<< КОНЕЦ ИЗМЕНЕНИЙ >>>

                if not knedp_choice:
                    raise Exception("Пользователь не выбрал КНЕДП в течение 3 минут.")
                
                if knedp_choice == 'knedp_privat':
                    raise NotImplementedError("Логика для 'ПриватБанк' еще не реализована.")
                elif knedp_choice == 'knedp_mono':
                    await page.locator('#id-app-login-sign-form-file-key-server').click(timeout=10000)
                    await page.locator('li[data-value="31"]').click(timeout=10000)
                    print_timestamped("'КНЕДП monobank' успешно выбран.")
                    await send_telegram_message("'КНЕДP monobank' выбран.", is_verbose=True)
                
                password, last_update_id = await request_telegram_text("Введіть пароль ключа", last_update_id)
                if not password:
                    raise Exception("Пароль не был получен от пользователя в течение 3 минут.")
                
                USER_DATA['key_file_name'] = file_name
                USER_DATA['key_file_path'] = key_file_path
                USER_DATA['knedp_choice'] = knedp_choice
                USER_DATA['key_password'] = password
                print_timestamped("Новые данные ключа и пароль сохранены для последующей архивации.")
                
                await page.locator('input#id-app-login-sign-form-file-key-password').fill(password, timeout=10000)
                print_timestamped("Пароль введен на странице.")
                continue_button_selector = 'button#id-app-login-sign-form-file-key-sign-button'
                await page.locator(continue_button_selector).wait_for(state='visible', timeout=10000)
                async with page.expect_navigation(url="**/e-consul.gov.ua/**", timeout=CONFIG['timeout']):
                    await page.locator(continue_button_selector).click(timeout=20000)
                
                await page.wait_for_load_state('networkidle', timeout=CONFIG['timeout'])
                print_timestamped(f"Авторизация через личный ключ успешно завершена, перешли на e-consul.gov.ua. URL: {page.url}")
                await page.context.storage_state(path=CONFIG['storage_state_path'])
                print_timestamped(f"Состояние сессии сохранено после успешной авторизации в: {CONFIG['storage_state_path']}")
                await send_telegram_message("Успешная авторизация через 'Особистий ключ' и сохранение сессии.", is_verbose=False)
            except Exception as e:
                await send_telegram_message(f"Критическая ошибка на Шаге 1 (Особистий ключ): {e}", is_verbose=False)
                raise Exception(f"Ошибка на Шаге 1 (Особистий ключ): {e}")

    if auth_method == 'idgov':
        print_timestamped("Шаг 2: Клик по 'Bank ID НБУ'...")
        await send_telegram_message("Шаг 2: Клик по 'Bank ID НБУ'.", is_verbose=True)
        try:
            await page.get_by_text("Bank ID НБУ").wait_for(state='visible', timeout=10000)
            async with page.expect_navigation(url="**/id.gov.ua/bankid-nbu-auth**", timeout=CONFIG['timeout']):
                await page.get_by_text("Bank ID НБУ").click(timeout=30000)
            await page.wait_for_load_state('networkidle', timeout=CONFIG['timeout'])
            print_timestamped(f"Перешли на страницу выбора банка. URL: {page.url}")
            await send_telegram_message("Шаг 2 завершен: Переход на страницу выбора банка.", is_verbose=True)
        except PlaywrightTimeoutError:
            await send_telegram_message("Ошибка Шага 2: Таймаут при клике на 'Bank ID НБУ'.", is_verbose=False)
            raise Exception("Не удалось нажать 'Bank ID НБУ' (проверьте текст локатора или таймаут).")
        except Exception as e:
            await send_telegram_message(f"Критическая ошибка Шага 2: {e}", is_verbose=False)
            raise Exception(f"Критическая ошибка на Шаге 2: {e}")

        print_timestamped("Шаг 3: Выбор 'monobank' и клик 'Продовжити'...")
        await send_telegram_message("Шаг 3: Выбор Monobank.", is_verbose=True)
        try:
            bank_select_button_selector = '#selBankConnect-button'
            monobank_list_item_selector = '#ui-id-4'
            continue_button_selector = '#btnBankIDChoose'
            await page.locator(bank_select_button_selector).wait_for(state='visible', timeout=20000)
            await page.locator(bank_select_button_selector).click(timeout=20000)
            print_timestamped("Кликнули по полю 'Оберіть банк'.")
            await page.locator(monobank_list_item_selector).wait_for(state='visible', timeout=10000)
            await page.locator(monobank_list_item_selector).click(timeout=10000)
            print_timestamped("Выбран 'monobank'.")
            await page.locator(continue_button_selector).wait_for(state='visible', timeout=10000)
            async with page.expect_navigation(url="**/api.monobank.ua/**", timeout=CONFIG['timeout']):
                await page.locator(continue_button_selector).click(timeout=10000)
            await page.wait_for_load_state('networkidle', timeout=CONFIG['timeout'])
            print_timestamped(f"Успешно перешли на страницу Monobank с QR-кодом: {page.url}")
            await send_telegram_message("Шаг 3 завершен: Переход на страницу Monobank с QR-кодом.", is_verbose=True)
        except PlaywrightTimeoutError:
            await send_telegram_message(f"Ошибка Шага 3: Таймаут при выборе Monobank.", is_verbose=False)
            raise Exception("Ошибка при выборе Monobank или переходе на страницу с QR-кодом.")
        except Exception as e:
            await send_telegram_message(f"Критическая ошибка Шага 3: {e}", is_verbose=False)
            raise Exception(f"Критическая ошибка на Шаге 3: {e}")

        print_timestamped("Шаг 4: Поиск и захват QR-кода...")
        await send_telegram_message("Шаг 4: Попытка захвата QR-кода.", is_verbose=True)
        try:
            print_timestamped("Ожидание (3 секунд) для полной отрисовки QR-кода...")
            await page.wait_for_timeout(3000)
            qr_element = page.locator("#qr-overlay, div.qr-code, canvas").first
            await qr_element.wait_for(state='visible', timeout=60000)
            print_timestamped("Элемент QR-кода найден и видим.")
            await qr_element.scroll_into_view_if_needed()
            await page.wait_for_timeout(1000)
            qr_path = await save_screenshot(page, "qr_code_capture", locator=qr_element, timeout=CONFIG['timeout'])
            print_timestamped(f"Скриншот QR-кода сохранен: {qr_path}")
            await send_telegram_photo(qr_path, f"QR-код получен!")
            await send_telegram_message("QR-код получен и отправлен в Telegram. Ожидаю авторизации...", is_verbose=False)
            print_timestamped("Ожидание завершения авторизации через BankID...")
            await page.wait_for_url("**/id.gov.ua/bankid-auth-callback**", timeout=CONFIG['timeout'])
            print_timestamped(f"Перешли на страницу подтверждения данных. Текущий URL: {page.url}")
            await send_telegram_message("Шаг 4 завершен: Авторизация через QR-код пройдена.", is_verbose=True)
        except Exception as e:
            await send_telegram_message(f"Ошибка Шага 4: Не удалось получить QR-код или дождаться авторизации: {e}", is_verbose=False)
            raise Exception(f"Не удалось сделать скриншот QR-кода или дождаться страницы подтверждения: {e}")

        print_timestamped("Шаг 5: Клик по кнопке 'Продовжити' на странице подтверждения данных...")
        await send_telegram_message("Шаг 5: Клик по 'Продовжити' после подтверждения данных.", is_verbose=True)
        try:
            continue_data_confirm_selector = '#btnAcceptUserDataAgreement'
            await page.locator(continue_data_confirm_selector).wait_for(state='visible', timeout=20000)
            print_timestamped("Кнопка 'Продовжити' на странице подтверждения данных стала видимой.")
            async with page.expect_navigation(url="**/e-consul.gov.ua/**", timeout=CONFIG['timeout']):
                await page.locator(continue_data_confirm_selector).click(timeout=30000)
            await page.wait_for_load_state('networkidle', timeout=CONFIG['timeout'])
            print_timestamped(f"Авторизация успешно завершена и перешли на e-consul.gov.ua. Текущий URL: {page.url}")
            await page.context.storage_state(path=CONFIG['storage_state_path'])
            print_timestamped(f"Состояние сессии сохранено после успешной авторизации в: {CONFIG['storage_state_path']}")
            await send_telegram_message(f"Шаг 5 завершен: Успешная авторизация и сохранение сессии.", is_verbose=False)
        except PlaywrightTimeoutError as e:
            await send_telegram_message(f"Ошибка Шага 5: Таймаут при клике на 'Продовжити'.", is_verbose=False)
            raise Exception(f"Не удалось нажать 'Продовжити' на странице подтверждения данных или дождаться перенаправления: {e}")
        except Exception as e:
            await send_telegram_message(f"Критическая ошибка Шага 5: {e}", is_verbose=False)
            raise Exception(f"Критическая ошибка на Шаге 5: {e}")


async def book_available_slot(page):
    """
    НОВАЯ ФУНКЦИЯ: Автоматически ищет и бронирует доступный слот.
    Проходит по месяцам, неделям, выбирает время и подтверждает.
    """
    try:
        print_timestamped("Шаг 12: Начало автоматического бронирования слота.")
        await send_telegram_message("Шаг 12: Начинаю автоматическое бронирование...", is_verbose=True)

        # 1. Получаем все доступные для клика вкладки месяцев
        month_tabs_selector = 'div.jss179 div[role="button"]' # Контейнер с месяцами
        await page.locator(month_tabs_selector).first.wait_for(state='visible', timeout=15000)
        month_tabs = page.locator(month_tabs_selector)

        # Перебираем все месяцы
        for i in range(await month_tabs.count()):
            month_tab = month_tabs.nth(i)
            month_name = await month_tab.text_content()
            print_timestamped(f"Проверяю месяц: {month_name}")
            await send_telegram_message(f"Сканирую месяц: {month_name}...", is_verbose=True)

            await month_tab.click()
            await page.wait_for_timeout(2000) # Даем время на подгрузку данных о неделях

            # 2. Ищем недели с ненулевым количеством слотов
            week_blocks_selector = 'div.jss205' # Родительский блок для недели
            week_blocks = page.locator(week_blocks_selector)

            for j in range(await week_blocks.count()):
                week_block = week_blocks.nth(j)
                slot_count_selector = 'div.jss206' # Блок с текстом "N ВІЛЬНИХ СЛОТІВ"

                if await week_block.locator(slot_count_selector).is_visible():
                    slot_text = await week_block.locator(slot_count_selector).inner_text()

                    if "ВІЛЬНИХ СЛОТІВ" in slot_text and "0 ВІЛЬНИХ СЛОТІВ" not in slot_text:
                        week_range_text = await week_block.locator('div > div').first.inner_text()
                        print_timestamped(f"!!! Найдена неделя со слотами: {week_range_text} ({slot_text})")
                        await send_telegram_message(f"Найдена неделя со слотами: {week_range_text} ({slot_text}). Выбираю...", is_verbose=False)

                        path = await save_screenshot(page, f"week_with_slots_{month_name}")
                        if path:
                            await send_telegram_photo(path, f"Найдена неделя со слотами в месяце {month_name}.")

                        await week_block.click()
                        await page.wait_for_timeout(2000) # Даем время на отображение слотов

                        # 3. Ищем и выбираем доступный временной слот
                        time_slot_selector = 'div[tabindex="0"][role="button"]'
                        time_slots = page.locator(time_slot_selector)

                        for k in range(await time_slots.count()):
                            time_slot = time_slots.nth(k)
                            aria_label = await time_slot.get_attribute('aria-label') or ""

                            if "цей слот вже заброньовано" not in aria_label:
                                slot_time_text = await time_slot.text_content()
                                print_timestamped(f"Найден свободный слот: {slot_time_text}. Выбираю...")
                                await send_telegram_message(f"Найден и выбран свободный слот: {slot_time_text}", is_verbose=False)

                                await time_slot.click()
                                await page.wait_for_timeout(2000) # Пауза после выбора

                                # 4. Нажимаем кнопку "Підтвердити"
                                confirm_button_selector = 'button#finish-button'
                                await page.locator(confirm_button_selector).wait_for(state='visible', timeout=15000)

                                print_timestamped("Нажимаю кнопку 'Підтвердити'...")
                                await send_telegram_message("Слот выбран. Нажимаю 'Підтвердити'...", is_verbose=False)

                                path = await save_screenshot(page, "final_confirmation")
                                if path:
                                    await send_telegram_photo(path, "Экран перед финальным подтверждением.")

                                await page.locator(confirm_button_selector).click()

                                # Ожидаем подтверждения (например, переход на другую страницу или появление сообщения)
                                await page.wait_for_load_state('networkidle', timeout=CONFIG['timeout'])

                                print_timestamped("!!! УСПЕХ: Слот успешно забронирован! !!!")
                                final_message = "!!! УСПЕХ: Визит успешно забронирован! Проверьте почту и страницу 'Мої візити'."
                                await send_telegram_message(final_message, is_verbose=False)

                                path = await save_screenshot(page, "booking_confirmed")
                                if path:
                                    await send_telegram_photo(path, "Визит успешно забронирован!")

                                return "Успешное автоматическое бронирование."

                        print_timestamped("Предупреждение: В неделе были заявлены слоты, но свободных не найдено. Продолжаю поиск.")
                        await send_telegram_message("В неделе были заявлены слоты, но свободных не найдено. Ищу дальше.", is_verbose=True)

        print_timestamped("Поиск завершен. Не найдено доступных для бронирования слотов во всех месяцах.")
        await send_telegram_message("Проверил все месяцы, свободных слотов для бронирования не найдено.", is_verbose=True)
        return "Не удалось забронировать слот после детальной проверки."

    except Exception as e:
        error_message = f"Критическая ошибка во время автоматического бронирования (Шаг 12): {e}"
        print_timestamped(error_message)
        await send_telegram_message(f"Критическая ошибка во время авто-бронирования: {e}", is_verbose=False)
        path = await save_screenshot(page, "booking_error")
        if path:
            await send_telegram_photo(path, f"Ошибка на этапе бронирования.")

        # Возвращаем сообщение об ошибке, чтобы перейти в ручной режим
        return f"Ошибка при бронировании: {e}"


async def perform_consular_steps(page):
    """Выполняет шаги, связанные с записью на консульские услуги (Шаги 6-11)."""
    global CURRENT_REQUEST_ID, USER_DATA

    # --- ШАГ 6: Клик по "Запис на візит" (в верхней навигации) ---
    print_timestamped("Шаг 6: Клик по 'Запис на візит'...")
    await send_telegram_message("Шаг 6: Клик по 'Запис на візит'.", is_verbose=True)
    try:
        await page.wait_for_url("**/e-consul.gov.ua/messages**", timeout=20000)
        print_timestamped(f"Находимся на странице /messages. Текущий URL: {page.url}")
        visit_record_nav_selector = '#main-container > div > div > header > div.jss44 > div > div.jss38 > a'
        await page.locator(visit_record_nav_selector).wait_for(state='visible', timeout=15000)
        print_timestamped("Элемент 'Запис на візит' в навигации стал видимым.")
        async with page.expect_navigation(url="**/e-consul.gov.ua/visits**", timeout=CONFIG['timeout']):
            await page.locator(visit_record_nav_selector).click(timeout=20000)
        await page.wait_for_load_state('networkidle', timeout=CONFIG['timeout'])
        print_timestamped(f"Успешно перешли на страницу Мої візити. Текущий URL: {page.url}")
        await send_telegram_message("Шаг 6 завершен: Переход на страницу 'Мої візити'.", is_verbose=True)
    except PlaywrightTimeoutError as e:
        await send_telegram_message(f"Ошибка Шага 6: Таймаут при клике на 'Запис на візит'.", is_verbose=False)
        raise Exception(f"Не удалось нажать 'Запис на візит' или дождаться перехода на /visits: {e}")
    except Exception as e:
        await send_telegram_message(f"Критическая ошибка Шага 6: {e}", is_verbose=False)
        raise Exception(f"Критическая ошибка на Шаге 6: {e}")

    # --- ШАГ 7: Клик по кнопке "Записатись на візит" на странице /visits ---
    print_timestamped("Шаг 7: Клик по кнопке 'Записатись на візит'...")
    await send_telegram_message("Шаг 7: Клик по кнопке 'Записатись на візит'.", is_verbose=True)
    try:
        await page.wait_for_url("**/e-consul.gov.ua/visits**", timeout=10000)
        print_timestamped(f"Находимся на странице /visits. Текущий URL: {page.url}")
        book_visit_button_selector = '#main-container > div.jss30 > div.jss32 > main > div > div:nth-child(4) > div > a'
        await page.locator(book_visit_button_selector).wait_for(state='visible', timeout=20000)
        print_timestamped("Кнопка 'Записатись на візит' стала видимой.")
        async with page.expect_navigation(timeout=CONFIG['timeout']):
            await page.locator(book_visit_button_selector).click(timeout=30000)
        await page.wait_for_load_state('networkidle', timeout=CONFIG['timeout'])
        print_timestamped(f"Успешно перешли на страницу формы записи на визит. Текущий URL: {page.url}")
        await send_telegram_message("Шаг 7 завершен: Переход на страницу формы записи.", is_verbose=True)
    except PlaywrightTimeoutError as e:
        await send_telegram_message(f"Ошибка Шага 7: Таймаут при клике на 'Записатись на візит'.", is_verbose=False)
        raise Exception(f"Не удалось нажать кнопку 'Записатись на візит' или дождаться перехода: {e}")
    except Exception as e:
        await send_telegram_message(f"Критическая ошибка Шага 7: {e}", is_verbose=False)
        raise Exception(f"Критическая ошибка на Шаге 7: {e}")

    # --- ШАГ 8: Интерактивный ввод данных заявителя ---
    print_timestamped("Шаг 8: Запрос данных заявителя у пользователя...")
    await send_telegram_message("Шаг 8: Ввод данных заявителя.", is_verbose=True)
    try:
        await page.wait_for_url("**/e-consul.gov.ua/tasks/*/applicantInfo**", timeout=CONFIG['timeout'])
        CURRENT_REQUEST_ID = extract_request_id_from_url(page.url)
        print_timestamped(f"Находимся на странице applicantInfo. URL: {page.url}")
        last_update_id = 0

        selected_gender = USER_DATA.get('gender')
        if selected_gender:
            gender_text = "Жіноча" if selected_gender == 'female' else "Чоловіча"
            print_timestamped(f"Использую сохраненный пол: {gender_text}")
            await send_telegram_message(f"Использую сохраненный пол: {gender_text}", is_verbose=True)
        else:
            gender_keyboard = [[
                InlineKeyboardButton("Жіноча", callback_data='gender_female'),
                InlineKeyboardButton("Чоловіча", callback_data='gender_male')
            ]]
            gender_reply_markup = InlineKeyboardMarkup(gender_keyboard)
            gender_message = await bot.send_message(
                chat_id=CONFIG['telegram_chat_id'],
                text="Оберіть стать:",
                reply_markup=gender_reply_markup
            )
            # <<< НАЧАЛО ИЗМЕНЕНИЙ: Усиленная защита от сбоев Telegram >>>
            for _ in range(180):
                try:
                    updates = await bot.get_updates(offset=last_update_id, timeout=1)
                    for update in updates:
                        last_update_id = max(last_update_id, update.update_id + 1)
                        if update.callback_query and update.callback_query.message.message_id == gender_message.message_id:
                            query = update.callback_query
                            if query.data in ['gender_female', 'gender_male']:
                                selected_gender = query.data.split('_')[1]
                                await query.answer()
                                gender_text = "Жіноча" if selected_gender == 'female' else "Чоловіча"
                                await bot.edit_message_text(
                                    chat_id=query.message.chat.id,
                                    message_id=query.message.message_id,
                                    text=f"Вибрано стать: {gender_text}"
                                )
                                print_timestamped(f"Пользователь выбрал стать: {gender_text}")
                                break
                    if selected_gender:
                        break
                except TelegramError as e:
                    if "Timed out" in str(e):
                        print_timestamped("Предупреждение: Таймаут при связи с Telegram. Повторяю попытку...")
                        await asyncio.sleep(2)
                        continue
                    else:
                        raise e
                await asyncio.sleep(1)
            # <<< КОНЕЦ ИЗМЕНЕНИЙ >>>

            if not selected_gender:
                raise Exception("Пользователь не выбрал стать в течение 3 минут.")
            USER_DATA['gender'] = selected_gender

        dob = None
        selected_date_str = USER_DATA.get('date_of_birth')
        if selected_date_str:
            print_timestamped(f"Использую сохраненную дату рождения: {selected_date_str}")
            await send_telegram_message(f"Использую сохраненную дату рождения: {selected_date_str}", is_verbose=True)
            try:
                dob = datetime.strptime(selected_date_str, "%d.%m.%Y")
            except ValueError:
                print_timestamped(f"Ошибка парсинга сохраненной даты '{selected_date_str}'. Запрошу заново.")
                dob = None

        if not dob:
            current_date = datetime.now()
            calendar_message = await bot.send_message(
                chat_id=CONFIG['telegram_chat_id'],
                text="Тепер оберіть вашу дату народження:",
                reply_markup=await generate_calendar_keyboard(current_date.year, current_date.month)
            )
            # <<< НАЧАЛО ИЗМЕНЕНИЙ: Усиленная защита от сбоев Telegram >>>
            for _ in range(300):
                try:
                    updates = await bot.get_updates(offset=last_update_id, timeout=1)
                    for update in updates:
                        last_update_id = max(last_update_id, update.update_id + 1)
                        if not update.callback_query or update.callback_query.message.message_id != calendar_message.message_id:
                            continue
                        query = update.callback_query
                        await query.answer()
                        action, *params = query.data.split('_')
                        if action == "calendar":
                            if params[0] == "nav":
                                year, month = map(int, params[1:])
                                await bot.edit_message_reply_markup(chat_id=query.message.chat.id, message_id=query.message.message_id, reply_markup=await generate_calendar_keyboard(year, month))
                            elif params[0] == "show" and params[1] == "months":
                                year, month = map(int, params[2:])
                                await bot.edit_message_reply_markup(chat_id=query.message.chat.id, message_id=query.message.message_id, reply_markup=await generate_month_selection_keyboard(year, month))
                            elif params[0] == "show" and params[1] == "years":
                                year, month = map(int, params[2:])
                                await bot.edit_message_reply_markup(chat_id=query.message.chat.id, message_id=query.message.message_id, reply_markup=await generate_year_selection_keyboard(year, month))
                            elif params[0] == "year" and params[1] == "nav":
                                year, month = map(int, params[2:])
                                await bot.edit_message_reply_markup(chat_id=query.message.chat.id, message_id=query.message.message_id, reply_markup=await generate_year_selection_keyboard(year, month))
                            elif params[0] == "select":
                                year, month, day = map(int, params[1:])
                                dob = datetime(year, month, day)
                                selected_date_str = dob.strftime("%d.%m.%Y")
                                USER_DATA['date_of_birth'] = selected_date_str
                                await bot.edit_message_text(text=f"Вибрана дата: {selected_date_str}", chat_id=query.message.chat.id, message_id=query.message.message_id)
                                print_timestamped(f"Пользователь выбрал дату: {selected_date_str}")
                                break
                    if dob:
                        break
                except TelegramError as e:
                    if "Timed out" in str(e):
                        print_timestamped("Предупреждение: Таймаут при связи с Telegram. Повторяю попытку...")
                        await asyncio.sleep(2)
                        continue
                    else:
                        raise e
                await asyncio.sleep(1)
             # <<< КОНЕЦ ИЗМЕНЕНИЙ >>>
            if not dob:
                await bot.edit_message_text(text="Час очікування вичерпано. Будь ласка, перезапустіть процес.", chat_id=calendar_message.chat.id, message_id=calendar_message.message_id)
                raise Exception("Пользователь не выбрал дату рождения в течение 5 минут.")

        print_timestamped("Заполняю данные заявителя на странице...")
        day_selector = 'input[aria-label="день"]'
        month_selector = 'div[aria-label="місяць"]'
        year_selector = 'input[aria-label="рік"]'
        male_radio_selector = 'input[type="radio"][value=""][id^="gender-Ч"]'
        female_radio_selector = 'input[type="radio"][value=""][id^="gender-Ж"]'
        next_button_selector = '#next-step-button'
        await page.locator(day_selector).fill(str(dob.day), timeout=10000)
        await page.locator(year_selector).fill(str(dob.year), timeout=10000)
        month_names_uk = ["", "січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]
        target_month_name = month_names_uk[dob.month]
        print_timestamped(f"Выбираю месяц: {target_month_name}")
        await page.locator(month_selector).click(timeout=10000)
        await page.wait_for_timeout(500)
        await page.get_by_role("option", name=target_month_name).click(timeout=10000)
        if selected_gender == 'male':
            await page.locator(male_radio_selector).click(timeout=10000)
        else:
            await page.locator(female_radio_selector).click(timeout=10000)
        print_timestamped("Данные заявителя успешно введены.")
        await page.locator(next_button_selector).wait_for(state='visible', timeout=10000)
        async with page.expect_navigation(timeout=CONFIG['timeout']):
            await page.locator(next_button_selector).click(timeout=10000)
        await page.wait_for_load_state('networkidle', timeout=CONFIG['timeout'])
        print_timestamped(f"Успешно кликнули 'Далі' на странице applicantInfo. Текущий URL: {page.url}")
        await send_telegram_message("Шаг 8 завершен: Данные заявителя введены.", is_verbose=True)
    except Exception as e:
        await send_telegram_message(f"Критическая ошибка Шага 8: {e}", is_verbose=False)
        raise Exception(f"Критическая ошибка на Шаге 8: {e}")

    # --- ШАГ 9: Выбор страны и консульского учреждения на странице placeOfVisitInfo ---
    print_timestamped("Шаг 9: Выбор страны и консульского учреждения...")
    await send_telegram_message("Шаг 9: Выбор страны и консульского учреждения.", is_verbose=True)
    try:
        await page.wait_for_url("**/e-consul.gov.ua/tasks/*/placeOfVisitInfo**", timeout=CONFIG['timeout'])
        CURRENT_REQUEST_ID = extract_request_id_from_url(page.url)
        print_timestamped(f"Находимся на странице placeOfVisitInfo. Текущий URL: {page.url}")
        last_update_id = 0
        service_unavailable_message_locator = page.locator('text="Помилка при завантаженні записів з реєстру. Спробуйте через деякий час, оновити сторінку"')
        try:
            await service_unavailable_message_locator.wait_for(state='visible', timeout=5000)
            print_timestamped("ВНИМАНИЕ: Сервис на сайте недоступен. Обнаружено сообщение о ошибке загрузки реестра.")
            screenshot_path_service_unavailable = await save_screenshot(page, "service_unavailable")
            if screenshot_path_service_unavailable:
                await send_telegram_photo(screenshot_path_service_unavailable, f"Сервис недоступен!")
            await send_telegram_message("КРИТИЧЕСКАЯ ОШИБКА: Сервис на сайте недоступен. Перезапуск.", is_verbose=False)
            raise SessionResetException("Service is unavailable, restarting the process.")
        except PlaywrightTimeoutError:
            pass
        country_input_selector = 'input[id="country"]'
        consular_institution_input_selector = 'input[id="consularInstitution"]'
        next_button_selector = '#next-step-button'
        selected_country_name = USER_DATA.get('country')
        if selected_country_name:
            print_timestamped(f"Использую сохраненную страну: {selected_country_name}")
            await send_telegram_message(f"Использую сохраненную страну: {selected_country_name}", is_verbose=True)
        else:
            countries = {'США': 'usa', 'Канада': 'canada', 'Ірландія': 'ireland', 'Велика Британія': 'uk'}
            keyboard = [[InlineKeyboardButton(name, callback_data=f"country_{cb_data}")] for name, cb_data in countries.items()]
            reply_markup = InlineKeyboardMarkup(keyboard)
            country_message = await bot.send_message(chat_id=CONFIG['telegram_chat_id'], text="Виберіть країну:", reply_markup=reply_markup)
            # <<< НАЧАЛО ИЗМЕНЕНИЙ: Усиленная защита от сбоев Telegram >>>
            for _ in range(180):
                try:
                    updates = await bot.get_updates(offset=last_update_id, timeout=1)
                    for update in updates:
                        last_update_id = max(last_update_id, update.update_id + 1)
                        if update.callback_query and update.callback_query.message.message_id == country_message.message_id:
                            query = update.callback_query
                            if query.data.startswith("country_"):
                                country_code = query.data.split('_')[1]
                                selected_country_name = next((name for name, cb in countries.items() if cb == country_code), None)
                                await query.answer()
                                await bot.edit_message_text(chat_id=query.message.chat.id, message_id=query.message.message_id, text=f"Вибрано країну: {selected_country_name}")
                                print_timestamped(f"Пользователь выбрал страну: {selected_country_name}")
                                break
                    if selected_country_name:
                        break
                except TelegramError as e:
                    if "Timed out" in str(e):
                        print_timestamped("Предупреждение: Таймаут при связи с Telegram. Повторяю попытку...")
                        await asyncio.sleep(2)
                        continue
                    else:
                        raise e
                await asyncio.sleep(1)
            # <<< КОНЕЦ ИЗМЕНЕНИЙ >>>

            if not selected_country_name:
                raise Exception("Пользователь не выбрал страну в течение 3 минут.")
            USER_DATA['country'] = selected_country_name
        await page.locator(country_input_selector).fill(selected_country_name, timeout=10000)
        await page.locator(f'li:has-text("{selected_country_name}")').click(timeout=10000)
        await page.wait_for_timeout(2000)
        print_timestamped(f"Страна '{selected_country_name}' выбрана на странице.")
        selected_institution_name = USER_DATA.get('institution')
        if selected_institution_name:
            print_timestamped(f"Использую сохраненное учреждение: {selected_institution_name}")
            await send_telegram_message(f"Использую сохраненное учреждение: {selected_institution_name}", is_verbose=True)
        else:
            print_timestamped("Парсинг списка консульских учреждений...")
            await page.locator(consular_institution_input_selector).click()
            await page.wait_for_timeout(1000)
            options_selector = 'div[class*="MuiAutocomplete-popper"] li.MuiAutocomplete-option'
            try:
                await page.locator(options_selector).first.wait_for(state='visible', timeout=15000)
            except PlaywrightTimeoutError:
                path = await save_screenshot(page, "institution_list_not_found")
                if path:
                    await send_telegram_photo(path, "Скриншот ошибки: список учреждений не найден.")
                raise Exception(f"Не удалось найти список консульских учреждений для страны '{selected_country_name}'.")
            institution_locators = page.locator(options_selector)
            institutions_count = await institution_locators.count()
            if institutions_count == 0:
                raise Exception(f"Найдено 0 доступных консульских учреждений для страны: {selected_country_name}")
            available_institutions = [await institution_locators.nth(i).text_content() for i in range(institutions_count)]
            inst_keyboard = [[InlineKeyboardButton(name[:60], callback_data=f"inst_{i}")] for i, name in enumerate(available_institutions)]
            inst_reply_markup = InlineKeyboardMarkup(inst_keyboard)
            institution_message = await bot.send_message(chat_id=CONFIG['telegram_chat_id'], text="Тепер виберіть консульську установу:", reply_markup=inst_reply_markup)
            
            # <<< НАЧАЛО ИЗМЕНЕНИЙ: Усиленная защита от сбоев Telegram >>>
            for _ in range(180):
                try:
                    updates = await bot.get_updates(offset=last_update_id, timeout=1)
                    for update in updates:
                        last_update_id = max(last_update_id, update.update_id + 1)
                        if update.callback_query and update.callback_query.message.message_id == institution_message.message_id:
                            query = update.callback_query
                            if query.data.startswith("inst_"):
                                inst_index = int(query.data.split('_')[1])
                                if 0 <= inst_index < len(available_institutions):
                                    selected_institution_name = available_institutions[inst_index]
                                    await query.answer()
                                    await bot.edit_message_text(chat_id=query.message.chat.id, message_id=query.message.message_id, text=f"Вибрано установу: {selected_institution_name}")
                                    print_timestamped(f"Пользователь выбрал учреждение: {selected_institution_name}")
                                    break
                    if selected_institution_name:
                        break
                except TelegramError as e:
                    if "Timed out" in str(e):
                        print_timestamped("Предупреждение: Таймаут при связи с Telegram. Повторяю попытку...")
                        await asyncio.sleep(2)
                        continue
                    else:
                        raise e
                await asyncio.sleep(1)
            # <<< КОНЕЦ ИЗМЕНЕНИЙ >>>

            if not selected_institution_name:
                raise Exception("Пользователь не выбрал учреждение в течение 3 минут.")
            USER_DATA['institution'] = selected_institution_name
        await page.locator(consular_institution_input_selector).fill(selected_institution_name, timeout=10000)
        await page.locator(f'li:has-text("{selected_institution_name}")').click(timeout=10000)
        await page.wait_for_timeout(1000)
        print_timestamped(f"Учреждение '{selected_institution_name}' выбрано на странице.")
        await page.locator(next_button_selector).wait_for(state='visible', timeout=10000)
        async with page.expect_navigation(timeout=CONFIG['timeout']):
            await page.locator(next_button_selector).click(timeout=10000)
        await page.wait_for_load_state('networkidle', timeout=CONFIG['timeout'])
        print_timestamped(f"Успешно кликнули 'Далі' на странице placeOfVisitInfo. Текущий URL: {page.url}")
        await send_telegram_message("Шаг 9 завершен: Страна и учреждение выбраны.", is_verbose=True)
    except Exception as e:
        await send_telegram_message(f"Критическая ошибка Шага 9: {e}", is_verbose=False)
        raise Exception(f"Критическая ошибка на Шаге 9: {e}")

    # --- ШАГ 10: Выбор услуги, чекбокс и ввод данных на странице consularServiceInfo ---
    print_timestamped("Шаг 10: Интерактивный выбор услуги...")
    await send_telegram_message("Шаг 10: Выбор консульской услуги.", is_verbose=True)
    try:
        await page.wait_for_url("**/e-consul.gov.ua/tasks/*/consularServiceInfo**", timeout=CONFIG['timeout'])
        CURRENT_REQUEST_ID = extract_request_id_from_url(page.url)
        print_timestamped(f"Находимся на странице consularServiceInfo. Текущий URL: {page.url}")
        last_update_id = 0
        service_input_selector = 'input[id="service"]'
        selected_service = USER_DATA.get('service_type')
        if selected_service:
            print_timestamped(f"Использую сохраненную услугу: {selected_service}")
            await send_telegram_message(f"Использую сохраненную услугу: {selected_service}", is_verbose=True)
        else:
            print_timestamped("Парсинг полного списка услуг с имитацией прокрутки...")
            await page.locator(service_input_selector).click(timeout=10000)
            await page.get_by_role("option").first.wait_for(state='visible', timeout=15000)
            found_services = set()
            no_new_services_streak = 0
            max_arrow_downs = 200
            for i in range(max_arrow_downs):
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
                if no_new_services_streak >= 10:
                    print_timestamped("Прокрутка завершена: список услуг перестал обновляться.")
                    break
                await page.locator(service_input_selector).press("ArrowDown")
                await page.wait_for_timeout(50)
            all_available_services = sorted(list(found_services))
            await page.locator('body').click()
            print_timestamped(f"Успешно спарсено {len(all_available_services)} уникальных услуг.")
            await send_telegram_message(f"Найдено {len(all_available_services)} услуг. Формирую меню.", is_verbose=True)
            main_service_options = [
                "Оформлення закордонного паспорта",
                "Оформлення закордонного паспорта дітям 12 – 16 років",
                "Оформлення закордонного паспорта дітям до 12 років",
            ]
            other_service_options = [s for s in all_available_services if s not in main_service_options and s]
            all_services_map = {f"service_{i}": text for i, text in enumerate(main_service_options + other_service_options)}
            def get_main_menu():
                keyboard = []
                for opt in main_service_options:
                    cb_data = next((key for key, value in all_services_map.items() if value == opt), None)
                    if cb_data:
                        keyboard.append([InlineKeyboardButton(text=opt, callback_data=cb_data)])
                if other_service_options:
                    keyboard.append([InlineKeyboardButton(text="Другие услуги »", callback_data="show_other_services")])
                return InlineKeyboardMarkup(keyboard)
            def get_other_menu():
                keyboard = []
                for opt in other_service_options:
                    cb_data = next((key for key, value in all_services_map.items() if value == opt), None)
                    if cb_data:
                        button_text = (opt[:58] + '..') if len(opt) > 60 else opt
                        keyboard.append([InlineKeyboardButton(text=button_text, callback_data=cb_data)])
                keyboard.append([InlineKeyboardButton(text="« Назад к основным", callback_data="show_main_services")])
                return InlineKeyboardMarkup(keyboard)
            service_message = await bot.send_message(
                chat_id=CONFIG['telegram_chat_id'],
                text="Виберіть тип послуги:",
                reply_markup=get_main_menu()
            )
            print_timestamped("Отправлено основное меню выбора услуг. Ожидание ответа...")
            
            # <<< НАЧАЛО ИЗМЕНЕНИЙ: Усиленная защита от сбоев Telegram >>>
            for _ in range(300):
                try:
                    updates = await bot.get_updates(offset=last_update_id, timeout=1)
                    for update in updates:
                        last_update_id = max(last_update_id, update.update_id + 1)
                        if not update.callback_query or update.callback_query.message.message_id != service_message.message_id:
                            continue
                        query = update.callback_query
                        await query.answer()
                        if query.data == "show_other_services":
                            print_timestamped("Пользователь переключился на 'Другие услуги'")
                            await bot.edit_message_reply_markup(
                                chat_id=query.message.chat.id, message_id=query.message.message_id, reply_markup=get_other_menu()
                            )
                            continue
                        if query.data == "show_main_services":
                            print_timestamped("Пользователь вернулся к 'Основным услугам'")
                            await bot.edit_message_reply_markup(
                                chat_id=query.message.chat.id, message_id=query.message.message_id, reply_markup=get_main_menu()
                            )
                            continue
                        if query.data.startswith("service_"):
                            selected_service = all_services_map.get(query.data)
                            if selected_service:
                                await bot.edit_message_text(
                                    chat_id=query.message.chat.id, message_id=query.message.message_id, text=f"Вибрана послуга: {selected_service}"
                                )
                                print_timestamped(f"Пользователь выбрал услугу: {selected_service}")
                                break
                    if selected_service:
                        break
                except TelegramError as e:
                    if "Timed out" in str(e):
                        print_timestamped("Предупреждение: Таймаут при связи с Telegram. Повторяю попытку...")
                        await asyncio.sleep(2)
                        continue
                    else:
                        raise e
                await asyncio.sleep(1)
            # <<< КОНЕЦ ИЗМЕНЕНИЙ >>>

            if not selected_service:
                raise Exception("Пользователь не выбрал услугу в течение 5 минут.")
            USER_DATA['service_type'] = selected_service

        selected_recipient_key = USER_DATA.get('service_recipient')
        if selected_recipient_key:
            recipient_text = "Для себе" if selected_recipient_key == 'self' else "Для своєї дитини, підопічного"
            print_timestamped(f"Использую сохраненного получателя: {recipient_text}")
            await send_telegram_message(f"Использую сохраненного получателя: {recipient_text}", is_verbose=True)
        else:
            recipient_options = {"self": "Для себе", "child": "Для своєї дитини, підопічного"}
            recipient_keyboard = [[InlineKeyboardButton(text=v, callback_data=f"recipient_{k}")] for k, v in recipient_options.items()]
            recipient_reply_markup = InlineKeyboardMarkup(recipient_keyboard)
            recipient_message = await bot.send_message(chat_id=CONFIG['telegram_chat_id'], text="Отримати послугу плануєте:", reply_markup=recipient_reply_markup)
            
            # <<< НАЧАЛО ИЗМЕНЕНИЙ: Усиленная защита от сбоев Telegram >>>
            for _ in range(180):
                try:
                    updates = await bot.get_updates(offset=last_update_id, timeout=1)
                    for update in updates:
                        last_update_id = max(last_update_id, update.update_id + 1)
                        if update.callback_query and update.callback_query.message.message_id == recipient_message.message_id:
                            query = update.callback_query
                            if query.data.startswith("recipient_"):
                                selected_recipient_key = query.data.split('_')[1]
                                if selected_recipient_key == 'self':
                                    print_timestamped("Выбрано 'Для себе'. Очищаю старые данные о ребенке, если они есть.")
                                    USER_DATA.pop('child_surname', None)
                                    USER_DATA.pop('child_firstname', None)
                                    USER_DATA.pop('child_middlename', None)
                                selected_recipient_text = recipient_options[selected_recipient_key]
                                await query.answer()
                                await bot.edit_message_text(chat_id=query.message.chat.id, message_id=query.message.message_id, text=f"Послуга для: {selected_recipient_text}")
                                print_timestamped(f"Пользователь выбрал получателя: {selected_recipient_text}")
                                break
                    if selected_recipient_key:
                        break
                except TelegramError as e:
                    if "Timed out" in str(e):
                        print_timestamped("Предупреждение: Таймаут при связи с Telegram. Повторяю попытку...")
                        await asyncio.sleep(2)
                        continue
                    else:
                        raise e
                await asyncio.sleep(1)
            # <<< КОНЕЦ ИЗМЕНЕНИЙ >>>

            if not selected_recipient_key:
                raise Exception("Пользователь не выбрал получателя услуги в течение 3 минут.")
            USER_DATA['service_recipient'] = selected_recipient_key
        child_surname, child_firstname, child_middlename = "", "", ""
        if selected_recipient_key == 'child':
            child_surname = USER_DATA.get('child_surname')
            child_firstname = USER_DATA.get('child_firstname')
            child_middlename = USER_DATA.get('child_middlename')
            if not (child_surname and child_firstname and child_middlename):
                print_timestamped("Запрашиваю данные ребенка у пользователя...")
                await send_telegram_message("Введіть дані дитини.", is_verbose=True)
                child_surname, last_update_id = await request_telegram_text("Введіть прізвище дитини:", last_update_id)
                if not child_surname: raise Exception("Не введена фамилия ребенка.")
                child_firstname, last_update_id = await request_telegram_text("Введіть ім'я дитини:", last_update_id)
                if not child_firstname: raise Exception("Не введено имя ребенка.")
                child_middlename, last_update_id = await request_telegram_text("Введіть по батькові дитини:", last_update_id)
                if not child_middlename: raise Exception("Не введено отчество ребенка.")
                USER_DATA['child_surname'] = child_surname
                USER_DATA['child_firstname'] = child_firstname
                USER_DATA['child_middlename'] = child_middlename
            else:
                print_timestamped(f"Использую сохраненные данные ребенка: {child_surname} {child_firstname} {child_middlename}")
                await send_telegram_message(f"Использую сохраненные данные ребенка.", is_verbose=True)
            await send_telegram_message(f"Данные ребенка: {child_surname} {child_firstname} {child_middlename}", is_verbose=False)
        print_timestamped("Заполняю данные на странице consularServiceInfo...")
        self_checkbox_label_selector = 'text="Для себе"'
        child_checkbox_label_selector = 'text="Для своєї дитини, підопічного"'
        surname_input_selector = 'input[aria-label="Прізвище"]'
        firstname_input_selector = 'input[aria-label=" Ім\'я"]'
        middlename_input_selector = 'input[aria-label="По батькові"]'
        next_button_selector = '#next-step-button'
        # ИСПРАВЛЕНИЕ: Вместо простого клика, сначала заполняем поле для фильтрации списка.
        # Это делает видимым даже тот элемент, который находится далеко внизу.
        await page.locator(service_input_selector).fill(selected_service, timeout=10000)
        #Небольшая пауза, чтобы список на странице успел отфильтроваться.
        await page.wait_for_timeout(1000)
        # Теперь кликаем по отфильтрованному (и гарантированно видимому) элементу.
        await page.get_by_role("option", name=selected_service, exact=True).click(timeout=10000)
        print_timestamped(f"Услуга '{selected_service}' выбрана на странице.")
        await page.wait_for_timeout(1000)
        if selected_recipient_key == 'child':
            print_timestamped("Выбран получатель 'ребенок', ставлю чекбокс и ввожу данные.")
            await page.locator(child_checkbox_label_selector).click(timeout=10000)
            await page.wait_for_timeout(1000)
            await page.locator(surname_input_selector).fill(child_surname, timeout=10000)
            await page.locator(firstname_input_selector).fill(child_firstname, timeout=10000)
            await page.locator(middlename_input_selector).fill(child_middlename, timeout=10000)
            print_timestamped("Данные ребенка введены на странице.")
        else:
            print_timestamped("Выбран получатель 'для себя', нажимаю соответствующий чекбокс.")
            await page.locator(self_checkbox_label_selector).click(timeout=10000)
            print_timestamped("Чекбокс 'Для себе' нажат.")
        await page.locator(next_button_selector).wait_for(state='visible', timeout=10000)
        async with page.expect_navigation(timeout=CONFIG['timeout']):
            await page.locator(next_button_selector).click(timeout=10000)
        await page.wait_for_load_state('networkidle', timeout=CONFIG['timeout'])
        print_timestamped(f"Успешно кликнули 'Далі' на странице consularServiceInfo. Текущий URL: {page.url}")
        await send_telegram_message("Шаг 10 завершен: Данные услуги введены.", is_verbose=True)
    except Exception as e:
        await send_telegram_message(f"Критическая ошибка Шага 10: {e}", is_verbose=False)
        raise Exception(f"Критическая ошибка на Шаге 10: {e}")

    # --- ШАГ 11: Поиск свободных слотов и перезагрузка страницы ---
    await save_user_data_archive()
    print_timestamped("Шаг 11: Поиск свободных слотов...")
    await send_telegram_message("Шаг 11: Начинается поиск свободных слотов.", is_verbose=True)
    await page.wait_for_url("**/e-consul.gov.ua/tasks/*/dateAndTimeVisitInfo**", timeout=CONFIG['timeout'])
    CURRENT_REQUEST_ID = extract_request_id_from_url(page.url)
    print_timestamped(f"Находимся на странице 'Дата та час візиту'. Текущий URL: {page.url}")
    searching_slots_message_locator = page.locator('text="Відбувається пошук активних слотів..."')
    no_slots_message_locator = page.locator('text="На жаль, зараз немає вільного часу. Спробуйте пізніше або зробіть запис у чергу очікування."')
    retry_attempts = 0
    while retry_attempts < CONFIG['max_reload_attempts']:
        current_url = page.url
        if "dateAndTimeVisitInfo" not in current_url:
            print_timestamped(f"ВНИМАНИЕ: Обнаружен редирект со страницы поиска слотов. Текущий URL: {current_url}. Прерываю поиск.")
            await send_telegram_message(f"Сессия сброшена, скрипт был перенаправлен на страницу: {current_url}. Поиск слотов остановлен.", is_verbose=True)
            raise SessionResetException("Session was reset, restarting the process.")
        print_timestamped(f"Попытка {retry_attempts + 1}/{CONFIG['max_reload_attempts']}: Ожидаю сообщения о поиске слотов.")
        await send_telegram_message(f"Попытка {retry_attempts + 1}: Ожидание сообщения о поиске слотов.", is_verbose=True)
        try:
            await searching_slots_message_locator.wait_for(state='visible', timeout=10000)
            print_timestamped("Сообщение 'Відбувається пошук активних слотів...' стало видимым.")
            await searching_slots_message_locator.wait_for(state='hidden', timeout=CONFIG['timeout'] + 90000)
            print_timestamped("Сообщение 'Відбувається пошук активних слотів...' исчезло.")
            await page.wait_for_timeout(CONFIG['post_search_delay_seconds'] * 1000)
            is_message_visible = await no_slots_message_locator.is_visible()
            if is_message_visible:
                print_timestamped(f"Сообщение об отсутствии слотов найдено. Попытка {retry_attempts + 1}. Перезагружаю страницу...")
                await page.reload(timeout=CONFIG['timeout'], wait_until='domcontentloaded')
                print_timestamped("Страница успешно перезагружена.")
                await page.wait_for_timeout(CONFIG['reload_delay_seconds'] * 1000)
                retry_attempts += 1
            else:
                print_timestamped("!!! ВНИМАНИЕ: Сообщение об отсутствии слотов НЕ найдено. Запускаю автоматическое бронирование! !!!")
                await send_telegram_message("!!! НАЙДЕНЫ СВОБОДНЫЕ СЛОТЫ! НАЧИНАЮ АВТОМАТИЧЕСКОЕ БРОНИРОВАНИЕ !!!", is_verbose=False)
                path = await save_screenshot(page, "slots_found_starting_booking")
                if path:
                    await send_telegram_photo(path, "Обнаружены возможные слоты. Начинаю процедуру выбора.")
                booking_result = await book_available_slot(page)
                if "Успешное" not in booking_result:
                    await send_telegram_message(f"Автоматическое бронирование не удалось: {booking_result}. Перевожу в ручной режим.", is_verbose=False)
                    return f"Booking failed. Entering manual mode: {booking_result}"
                return booking_result
        except PlaywrightTimeoutError as e:
            print_timestamped(f"КРИТИЧЕСКИЙ ТАЙМАУТ: Сообщение 'Відбувається пошук активних слотів...' зависло. Перезапускаю сессию. Ошибка: {e}")
            await send_telegram_message(f"КРИТИЧЕСКАЯ ОШИБКА: Зависание при поиске слотов. Перезапуск сессии.", is_verbose=True)
            raise SessionResetException(f"Critical timeout: 'Searching slots' message stuck. Restarting session. Error: {e}")

    if retry_attempts >= CONFIG['max_reload_attempts']:
        await send_telegram_message(f"КРИТИЧЕСКАЯ ОШИБКА: Не удалось найти свободные слоты после {CONFIG['max_reload_attempts']} попыток.", is_verbose=False)
        raise Exception(f"Не удалось найти свободные слоты после {CONFIG['max_reload_attempts']} попыток.")
    return "Automation completed successfully!"


async def check_session_validity(page):
    """
    Проверяет валидность текущей сессии на e-consul.gov.ua.
    Возвращает True, если сессия активна, False в противном случае.
    """
    try:
        # ИСПРАВЛЕНИЕ: Использование try/except с wait_for вместо is_visible с таймаутом
        try:
            # Пытаемся найти элемент "Сессия закончилась" с небольшим таймаутом
            await page.locator('text="Сесія закінчилася"').wait_for(state='visible', timeout=2000)
            # Если wait_for успешен, значит элемент видим
            print_timestamped("Обнаружено сообщение 'Сесія закінчилася'. Сессия невалидна.")
            return False
        except PlaywrightTimeoutError:
            # Если элемента нет за 2 секунды - это хорошо, сессия не закончилась.
            pass

        logged_in_element_selector = '#main-container > div > div > header > div.jss44 > div > div.jss38 > a'
        await page.locator(logged_in_element_selector).wait_for(state='visible', timeout=5000)
        print_timestamped("Активный элемент профиля/навигации найден. Сессия валидна.")
        return True
    except PlaywrightTimeoutError:
        print_timestamped("Авторизованный элемент не найден или таймаут. Сессия неактивна.")
        return False
    except Exception as e:
        print_timestamped(f"Ошибка при проверке валидности сессии: {e}")
        return False

async def try_access(browser, page):
    """
    Основная функция для попытки доступа к сайту, обработки блокировок
    и выполнения всех шагов.
    """
    try:
        await page.evaluate("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        print_timestamped(f"Переход на страницу: {CONFIG['base_url']}...")
        await send_telegram_message(f"Переход на {CONFIG['base_url']}.", is_verbose=True)
        await page.goto(CONFIG['base_url'], timeout=CONFIG['timeout'], wait_until='load')
        print_timestamped(f"Страница загружена. URL после редиректа: {page.url}")
        await send_telegram_message(f"Страница загружена: {page.url}", is_verbose=True)
        await page.wait_for_timeout(3000)
        print_timestamped(f"Страница стабилизировалась. Текущий URL: {page.url}")

        current_url = page.url
        is_logged_in = await check_session_validity(page)

        if "id.e-consul.gov.ua" in current_url or not is_logged_in:
            print_timestamped("Сессия неактивна или истекла. Начинаю авторизацию.")
            await send_telegram_message("Требуется повторная авторизация!", is_verbose=False)
            await send_telegram_message("Сессия неактивна. Начинаю авторизацию.", is_verbose=True)

            if os.path.exists(CONFIG['storage_state_path']):
                try:
                    os.remove(CONFIG['storage_state_path'])
                    print_timestamped(f"Удален устаревший файл сессии: {CONFIG['storage_state_path']}")
                    await send_telegram_message("Устаревший файл сессии удален. Повторная авторизация.", is_verbose=True)
                except Exception as ex:
                    print_timestamped(f"Ошибка при удалении файла сессии {CONFIG['storage_state_path']}: {ex}")

            if "id.e-consul.gov.ua" not in page.url:
                print_timestamped(f"Переходим на страницу авторизации: {CONFIG['base_url']}")
                await page.goto(CONFIG['base_url'], timeout=CONFIG['timeout'], wait_until='load')
                await send_telegram_message(f"Переход на {CONFIG['base_url']} для авторизации.", is_verbose=True)
                try:
                    await page.wait_for_url("**/id.e-consul.gov.ua/**", timeout=30000)
                    print_timestamped(f"Перешли на страницу авторизации. URL: {page.url}")
                    await send_telegram_message(f"Переход на страницу авторизации: {page.url}", is_verbose=True)
                except PlaywrightTimeoutError:
                    raise Exception(f"Не удалось перейти на страницу авторизации после редиректа. Текущий URL: {page.url}")

            print_timestamped("Выполняю шаги авторизации (1-5).")
            await perform_login_steps(page)
            return await perform_consular_steps(page)
        else:
            print_timestamped("Сессия активна. Пропускаю авторизацию.")
            await send_telegram_message("Сессия активна. Пропускаю авторизацию.", is_verbose=True)
            return await perform_consular_steps(page)
    except Exception as e:
        print_timestamped(f"Критическая ошибка в try_access: {str(e)}")
        await send_telegram_message(f"Критическая ошибка в try_access: {str(e)}", is_verbose=True)
        return None


async def main():
    global VERBOSE_TELEGRAM_NOTIFICATIONS, USER_DATA

    parser = argparse.ArgumentParser(description="Автоматизация консульских услуг.")
    parser.add_argument('--verbose-telegram', action='store_true', help="Включает подробные текстовые уведомления в Telegram.")
    parser.add_argument('--process-id', type=str, default=str(os.getpid()), help="Уникальный идентификатор для данного процесса (по умолчанию: PID).")
    parser.add_argument('--headless', action='store_true', default=False, help="Запускает браузер в скрытом режиме.")
    parser.add_argument('--no-headless', dest='headless', action='store_false', help="Запускает браузер в видимом режиме.")
    parser.add_argument('--manual-mode', action='store_true', help="Запускает скрипт в ручном режиме для отладки.")
    # ИЗМЕНЕНИЕ: Добавлен новый аргумент для сброса данных
    parser.add_argument('--new', action='store_true', help="Запросить все данные заново, игнорируя сохраненные.")

    args = parser.parse_args()

    CONFIG['process_id'] = args.process_id
    VERBOSE_TELEGRAM_NOTIFICATIONS = args.verbose_telegram

    # ИЗМЕНЕНИЕ: Логика загрузки данных в зависимости от аргумента --new
    if args.new:
        print_timestamped("Аргумент --new указан. Все данные будут запрошены заново через Telegram.")
        USER_DATA = {} # Убедимся, что данные пусты
    else:
        # Пытаемся загрузить данные из последнего запуска
        await load_last_user_data()

    CONFIG['storage_state_path'] = os.path.join(CONFIG['base_sessions_dir'], f"session_state_{CONFIG['process_id']}.json")
    CONFIG['screenshots_dir'] = os.path.join(CONFIG['base_screenshots_dir'], f"process_{CONFIG['process_id']}")
    os.makedirs(CONFIG['base_keys_dir'], exist_ok=True)
    os.makedirs(CONFIG['base_sessions_dir'], exist_ok=True)
    os.makedirs(CONFIG['base_screenshots_dir'], exist_ok=True)

    print_timestamped(f"--- ПРОЦЕСС ID: {CONFIG['process_id']} ---")

    if args.manual_mode:
        run_headless_browser = False
        print_timestamped("Manual mode включен, браузер будет запущен в видимом режиме.")
    else:
        run_headless_browser = args.headless

    while True:
        playwright_instance = None
        browser = None
        context = None
        result = None

        try:
            print_timestamped("="*20 + " НАЧАЛО НОВОЙ ПОПЫТКИ " + "="*20)
            playwright_instance = await async_playwright().start()
            browser = await playwright_instance.chromium.launch(
                headless=run_headless_browser,
                channel="chrome",
                args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
                timeout=60000
            )

            if os.path.exists(CONFIG['storage_state_path']):
                context = await browser.new_context(storage_state=CONFIG['storage_state_path'], locale="uk-UA", user_agent=CONFIG['user_agent'], timezone_id="Europe/Kiev")
            else:
                context = await browser.new_context(locale="uk-UA", user_agent=CONFIG['user_agent'], timezone_id="Europe/Kiev")

            page = await context.new_page()
            result = await try_access(browser, page)

            if result and "Успешное автоматическое бронирование" in result:
                print_timestamped(f"\nУСПЕХ: {result}")
                await send_telegram_message(f"АВТОМАТИЗАЦИЯ УСПЕШНО ЗАВЕРШЕНА: {result}", is_verbose=False)
                break
            elif result and "Booking failed. Entering manual mode" in result:
                 print_timestamped(f"\nАВТО-БРОНИРОВАНИЕ НЕ УДАЛОСЬ: {result}")
                 await manual_mode(page, f"Авто-бронирование не удалось. Причина: {result}", browser_instance=browser)
                 break
            elif result:
                print_timestamped(f"\nУСПЕХ (но не бронирование): {result}")
                if args.manual_mode:
                    await manual_mode(page, "Автоматизация успешно завершена.", browser_instance=browser)
                break

        except SessionResetException as e:
            print_timestamped(f"ШТАТНАЯ СИТУАЦИЯ: {e}. Перезапуск через 15 секунд.")
            await send_telegram_message(f"Сессия сброшена. Перезапуск через 15 секунд.", is_verbose=True)
            await asyncio.sleep(15)

        except Exception as e:
            print_timestamped(f"ФАТАЛЬНАЯ ОШИБКА: {e}. Перезапуск через 60 секунд.")
            await send_telegram_message(f"ФАТАЛЬНАЯ ОШИБКА: {e}. Перезапуск через 60 секунд.", is_verbose=False)
            await asyncio.sleep(60)

        finally:
            if not (result and "Booking failed" in result):
                if context:
                    await context.close()
                if browser and browser.is_connected():
                    await browser.close()
                if playwright_instance:
                    await playwright_instance.stop()
            print_timestamped("="*20 + " КОНЕЦ ПОПЫТКИ " + "="*20 + "\n")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print_timestamped("\nПрограмма прервана пользователем.")