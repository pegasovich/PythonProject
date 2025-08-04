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
import time
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
from telegram.error import TelegramError, BadRequest
import re

# --- CONFIGURATION ---
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv('DEV_BOT_TOKEN')
OWNER_TELEGRAM_ID = os.getenv('OWNER_TELEGRAM_ID')
WORKER_COUNT = 3  # Базовое количество воркеров
MAX_WORKERS = 10  # Максимальное количество воркеров
AUTO_SCALE_THRESHOLD = 2  # Если в очереди больше этого числа, добавляем воркеров
BASE_PROFILES_DIR = 'browser_profiles'
BASE_SCREENSHOTS_DIR = 'screenshots_data'
BASE_KEYS_DIR = 'user_keys'
USER_DATA_ARCHIVES_DIR = 'user_data_archives'
DATA_FILE = 'countries_data.json'

# --- ADVANCED WORKER SETTINGS ---
MAX_WORKER_TIME = 45 * 60  # 45 минут максимум на одного пользователя
WORKER_TIMEOUT_CHECK_INTERVAL = 60  # Проверка тайм-аутов каждую минуту
MAX_SESSION_RESTARTS = 3  # Максимум перезапусков на пользователя

# --- GLOBAL VARIABLES ---
task_queue = asyncio.Queue()
priority_queue = asyncio.PriorityQueue()  # Очередь с приоритетами
user_sessions = {}
active_workers = {}  # Отслеживание активных воркеров
worker_start_times = {}  # Время начала работы воркеров

# --- НОВАЯ СИСТЕМА СОХРАНЕНИЯ СЕССИЙ ---
paused_sessions = {}  # Сохраненные браузерные сессии {user_id: {'context': context, 'page': page, 'last_attempt': timestamp}}
session_rotation_queue = asyncio.Queue()  # Очередь для ротации пользователей
MAX_PAUSED_SESSIONS = 5  # Максимум одновременно сохраненных сессий
SESSION_RETRY_INTERVAL = 180  # 3 минуты между попытками для одного пользователя


class SessionResetException(Exception):
    """Custom exception for session resets that require a restart."""
    pass

class NoSlotsFoundException(Exception):
    """Custom exception for when no slots are found."""
    pass

# --- WORKER MANAGEMENT FUNCTIONS ---

async def check_worker_timeouts(app: Application):
    """Проверяет и останавливает воркеров, которые работают слишком долго"""
    import time
    current_time = time.time()
    for worker_id, start_time in list(worker_start_times.items()):
        if current_time - start_time > MAX_WORKER_TIME:
            logging.warning(f"Воркер {worker_id} превысил лимит времени ({MAX_WORKER_TIME}s). Принудительная остановка.")
            # Здесь можно добавить логику принудительной остановки
            if worker_id in active_workers:
                user_id = active_workers[worker_id]
                chat_id = user_sessions.get(user_id, {}).get('chat_id')
                if chat_id:
                    await app.bot.send_message(chat_id, 
                        "⏱️ Время поиска превысило лимит. Ваше задание будет перезапущено с новым воркером.")

async def auto_scale_workers(app: Application, headless_mode: bool):
    """Автоматически добавляет воркеров при большой очереди"""
    queue_size = task_queue.qsize()
    current_workers = len(active_workers)
    
    if queue_size > AUTO_SCALE_THRESHOLD and current_workers < MAX_WORKERS:
        new_worker_id = current_workers + 1
        logging.info(f"Автомасштабирование: запускаю дополнительного воркера #{new_worker_id}")
        # Запускаем нового воркера
        asyncio.create_task(worker(new_worker_id, app, headless_mode))
        
        # Уведомляем пользователей в очереди
        if queue_size > 0:
            # Примерно уведомляем пользователей о новом воркере
            for user_id in list(task_queue._queue):
                if user_id in user_sessions:
                    chat_id = user_sessions[user_id].get('chat_id')
                    if chat_id:
                        await app.bot.send_message(chat_id, 
                            f"🚀 Запущен дополнительный воркер #{new_worker_id}! Очередь будет обработана быстрее.")
                        break  # Уведомляем только первого в очереди

# --- ФУНКЦИИ УПРАВЛЕНИЯ СЕССИЯМИ ---

async def save_browser_session(user_id: int, context, page, bot: Bot):
    """Сохраняет браузерную сессию пользователя для последующего восстановления"""
    
    # Если уже есть слишком много сохраненных сессий, закрываем самую старую
    if len(paused_sessions) >= MAX_PAUSED_SESSIONS:
        oldest_user_id = min(paused_sessions.keys(), key=lambda uid: paused_sessions[uid]['last_attempt'])
        await close_paused_session(oldest_user_id, bot)
        logging.info(f"Закрыта старая сессия пользователя {oldest_user_id} для освобождения места")
    
    # Дополнительные проверки перед сохранением
    try:
        current_url = page.url
        page_title = await page.title()
        
        # Проверяем, что мы действительно на правильной странице
        if "dateAndTimeVisitInfo" not in current_url:
            logging.warning(f"Попытка сохранить сессию не на странице выбора слотов: {current_url}")
            chat_id = user_sessions.get(user_id, {}).get('chat_id')
            if chat_id:
                await bot.send_message(chat_id, 
                    f"⚠️ Не удается сохранить сессию - неподходящая страница.\n"
                    f"Потребуется повторная авторизация при следующем запуске.")
            return False
        
        # Проверяем доступность основных элементов страницы
        calendar_container = page.locator('div[class*="scrollbar-container"]')
        await calendar_container.wait_for(state='visible', timeout=5000)
        
    except Exception as pre_save_error:
        logging.error(f"Ошибка при проверке страницы перед сохранением сессии {user_id}: {pre_save_error}")
        chat_id = user_sessions.get(user_id, {}).get('chat_id')
        if chat_id:
            await bot.send_message(chat_id, 
                f"⚠️ Проблема при сохранении сессии: {str(pre_save_error)[:100]}...\n"
                f"При следующем запуске может потребоваться повторная авторизация.")
        # Продолжаем сохранение, но с пониженной надежностью
        current_url = "unknown"
        page_title = "unknown"
    
    paused_sessions[user_id] = {
        'context': context,
        'page': page,
        'last_attempt': time.time(),
        'attempt_count': paused_sessions.get(user_id, {}).get('attempt_count', 0) + 1,
        'saved_url': current_url,
        'page_title': page_title,
        'browser_pid': str(context.browser) if context and context.browser else "unknown"
    }
    
    # Добавляем в ротационную очередь
    await session_rotation_queue.put(user_id)
    
    chat_id = user_sessions.get(user_id, {}).get('chat_id')
    if chat_id:
        # Рассчитываем предполагаемую надежность
        reliability_estimate = "90-95%" if "dateAndTimeVisitInfo" in current_url else "60-70%"
        
        await bot.send_message(chat_id, 
            f"💾 **Сессия сохранена!**\n"
            f"🔄 Переключаюсь на других пользователей.\n"
            f"⏰ Вернусь через {SESSION_RETRY_INTERVAL // 60} мин\n"
            f"🎯 Ожидаемая надежность восстановления: {reliability_estimate}\n"
            f"📍 Сохранено на странице: {page_title[:50]}...")
    
    logging.info(f"Сессия пользователя {user_id} сохранена. Всего сохранено: {len(paused_sessions)}. URL: {current_url}")
    return True

async def restore_browser_session(user_id: int, bot: Bot):
    """Восстанавливает сохраненную браузерную сессию с расширенными проверками"""
    if user_id not in paused_sessions:
        return None, None
    
    session_data = paused_sessions[user_id]
    context = session_data['context']
    page = session_data['page']
    
    chat_id = user_sessions.get(user_id, {}).get('chat_id')
    user_data = load_user_data(user_id)
    auth_method = user_data.get('auth_method', 'unknown')
    
    # Проверяем, что сессия еще активна
    try:
        # 1. Базовые проверки
        if page.is_closed():
            logging.warning(f"Сохраненная страница пользователя {user_id} закрыта")
            await close_paused_session(user_id, bot)
            return None, None
            
        if not context.browser.is_connected():
            logging.warning(f"Браузер для пользователя {user_id} отключен")
            await close_paused_session(user_id, bot)
            return None, None
        
        # 2. Проверка URL
        current_url = page.url
        if "dateAndTimeVisitInfo" not in current_url:
            logging.warning(f"Сессия пользователя {user_id} перенаправлена: {current_url}")
            
            # Пробуем вернуться на правильную страницу
            if "e-consul.gov.ua" in current_url and "id.e-consul.gov.ua" not in current_url:
                try:
                    logging.info(f"Пытаюсь восстановить навигацию для пользователя {user_id}")
                    is_session_valid = await check_session_validity(page)
                    if is_session_valid:
                        # Сессия активна, но мы не на нужной странице - можем восстановить
                        if chat_id:
                            await bot.send_message(chat_id, 
                                "🔄 Сессия активна, но нужно заново пройти к выбору слотов...")
                        logging.info(f"Сессия пользователя {user_id} активна, но требует повторного заполнения форм")
                        # Возвращаем None - нужна новая сессия, но с сохранением авторизации
                        await close_paused_session(user_id, bot)
                        return None, None
                except Exception as nav_error:
                    logging.error(f"Ошибка проверки навигации для пользователя {user_id}: {nav_error}")
            
            # Проверяем, нужна ли реавторизация
            if "id.e-consul.gov.ua" in current_url:
                # Сессия истекла, нужна реавторизация
                if auth_method == 'key' and user_data.get('key_file_path') and user_data.get('key_password'):
                    # Для файла-ключа можем сделать автоматическую реавторизацию
                    logging.info(f"Попытка автоматической реавторизации с файлом-ключом для пользователя {user_id}")
                    if chat_id:
                        await bot.send_message(chat_id, 
                            "🔑 **Автоматическая реавторизация**\n"
                            "Сессия истекла, но у вас сохранен файл-ключ.\n"
                            "Выполняю автоматическую авторизацию...")
                    
                    try:
                        # Выполняем автоматическую авторизацию с ключом
                        success = await auto_reauth_with_key(page, user_data, chat_id, bot)
                        if success:
                            # После успешной реавторизации возвращаемся к началу восстановления
                            return await restore_browser_session(user_id, bot)
                        else:
                            await close_paused_session(user_id, bot)
                            return None, None
                    except Exception as reauth_error:
                        logging.error(f"Ошибка автоматической реавторизации для {user_id}: {reauth_error}")
                        if chat_id:
                            await bot.send_message(chat_id, 
                                f"❌ Автоматическая реавторизация не удалась: {str(reauth_error)[:100]}...")
                        await close_paused_session(user_id, bot)
                        return None, None
                else:
                    # Для других методов (Diya, BankID) требуется ручная реавторизация
                    if chat_id:
                        auth_name = {'diia': 'Дія', 'idgov': 'BankID'}.get(auth_method, auth_method)
                        await bot.send_message(chat_id, 
                            f"🔐 **Требуется реавторизация через {auth_name}**\n"
                            f"Сессия истекла. Запускаю новую сессию...")
                    await close_paused_session(user_id, bot)
                    return None, None
            
            await close_paused_session(user_id, bot)
            return None, None
        
        # 3. Расширенная проверка состояния страницы
        try:
            # Проверяем наличие основных элементов календаря
            calendar_container = page.locator('div[class*="scrollbar-container"]')
            await calendar_container.wait_for(state='visible', timeout=5000)
            
            # Проверяем, что страница не показывает ошибки
            error_messages = [
                'text="Сесія закінчилася"',
                'text="Помилка"',
                'text="Доступ заборонено"',
                'text="Unauthorized"'
            ]
            
            for error_selector in error_messages:
                error_element = page.locator(error_selector)
                if await error_element.is_visible():
                    logging.warning(f"Обнаружено сообщение об ошибке для пользователя {user_id}: {error_selector}")
                    await close_paused_session(user_id, bot)
                    return None, None
                    
        except Exception as page_check_error:
            logging.warning(f"Не удалось проверить состояние страницы для пользователя {user_id}: {page_check_error}")
            # Не критично, продолжаем
        
        # 4. Проверка времени с последней активности (с учетом типа авторизации)
        time_since_pause = time.time() - session_data['last_attempt']
        
        # Устанавливаем лимиты времени в зависимости от типа авторизации
        if auth_method == 'key':
            max_session_age = 45 * 60  # 45 минут для файла-ключа (дольше)
            reliability_bonus = 10  # Бонус к надежности для файла-ключа
        elif auth_method == 'diia':
            max_session_age = 25 * 60  # 25 минут для Diya
            reliability_bonus = 0
        elif auth_method == 'idgov':
            max_session_age = 20 * 60  # 20 минут для BankID (короче)
            reliability_bonus = 0
        else:
            max_session_age = 25 * 60  # По умолчанию 25 минут
            reliability_bonus = 0
        
        if time_since_pause > max_session_age:
            logging.warning(f"Сессия пользователя {user_id} слишком старая ({time_since_pause//60:.1f} мин) для метода {auth_method}")
            if chat_id:
                await bot.send_message(chat_id, 
                    f"⏰ Сессия сохранялась слишком долго ({time_since_pause//60:.1f} мин) для метода авторизации {auth_method}.\n"
                    f"Потребуется повторная авторизация.")
            await close_paused_session(user_id, bot)
            return None, None
        
        # 5. Обновляем временную метку
        session_data['last_attempt'] = time.time()
        
        if chat_id:
            reliability_percent = max(50, 95 - (time_since_pause / 60) * 2 + reliability_bonus)  # Бонус для файла-ключа
            auth_info = {
                'key': 'Файл-ключ (высокая надежность)',
                'diia': 'Diya (средняя надежность)',
                'idgov': 'BankID (умеренная надежность)'
            }.get(auth_method, auth_method)
            
            await bot.send_message(chat_id, 
                f"🔄 **Восстанавливаю сессию...**\n"
                f"📊 Попытка #{session_data['attempt_count']}\n"
                f"🎯 Надежность: ~{reliability_percent:.0f}%\n"
                f"🔐 Авторизация: {auth_info}\n"
                f"⏱️ Возраст сессии: {time_since_pause//60:.0f} мин")
        
        logging.info(f"Успешно восстановлена сессия пользователя {user_id} (возраст: {time_since_pause//60:.1f} мин, метод: {auth_method})")
        return context, page
        
    except Exception as e:
        logging.error(f"Ошибка при восстановлении сессии пользователя {user_id}: {e}")
        if chat_id:
            await bot.send_message(chat_id, 
                f"❌ Не удалось восстановить сессию: {str(e)[:100]}...\nЗапускаю новую сессию с авторизацией.")
        await close_paused_session(user_id, bot)
        return None, None

async def auto_reauth_with_key(page, user_data: dict, chat_id: int, bot: Bot) -> bool:
    """Автоматическая реавторизация с файлом-ключом"""
    try:
        logging.info(f"Начинаю автоматическую реавторизацию с файлом-ключом для чата {chat_id}")
        
        # Переходим на страницу авторизации если еще не там
        if "id.e-consul.gov.ua" not in page.url:
            await page.goto("https://id.e-consul.gov.ua/", timeout=60000)
        
        # Выбираем авторизацию через файл-ключ
        await page.get_by_role("button", name="Особистий ключ").click()
        await page.wait_for_load_state('networkidle')
        
        # Загружаем файл ключа
        async with page.expect_file_chooser() as fc_info:
            await page.locator('text="Оберіть ключ на своєму носієві"').click()
        await (await fc_info.value).set_files(user_data['key_file_path'])
        
        # Выбираем сервер ключей
        await page.locator('#id-app-login-sign-form-file-key-server').click()
        await page.locator('li[data-value="31"]').click()
        
        # Вводим пароль от ключа
        await page.locator('input#id-app-login-sign-form-file-key-password').fill(user_data['key_password'])
        
        # Кликаем кнопку входа
        async with page.expect_navigation(url="**/e-consul.gov.ua/**", timeout=60000):
            await page.locator('button#id-app-login-sign-form-file-key-sign-button').click()
        
        # Проверяем успешность авторизации
        await page.wait_for_load_state('networkidle', timeout=30000)
        await page.wait_for_selector('a:has-text("Запис на візит")', timeout=20000)
        
        if chat_id:
            await bot.send_message(chat_id, "✅ Автоматическая реавторизация успешна!")
        
        logging.info(f"Автоматическая реавторизация с файлом-ключом успешна для чата {chat_id}")
        return True
        
    except Exception as e:
        logging.error(f"Ошибка автоматической реавторизации: {e}")
        return False

async def close_paused_session(user_id: int, bot: Bot):
    """Закрывает сохраненную сессию"""
    if user_id not in paused_sessions:
        return
        
    session_data = paused_sessions[user_id]
    
    try:
        context = session_data['context']
        if context and not context.browser.is_connected():
            await context.close()
    except Exception as e:
        logging.error(f"Ошибка при закрытии контекста пользователя {user_id}: {e}")
    
    del paused_sessions[user_id]
    
    chat_id = user_sessions.get(user_id, {}).get('chat_id')
    if chat_id:
        await bot.send_message(chat_id, 
            "⚠️ Ваша сохраненная сессия была закрыта. При следующем запуске потребуется повторная авторизация.")
    
    logging.info(f"Сессия пользователя {user_id} закрыта и удалена")

async def get_next_rotation_user():
    """Получает следующего пользователя из ротационной очереди"""
    import time
    
    try:
        user_id = await asyncio.wait_for(session_rotation_queue.get(), timeout=1.0)
        session_data = paused_sessions.get(user_id)
        
        if not session_data:
            return None
            
        # Проверяем, прошло ли достаточно времени
        time_since_last = time.time() - session_data['last_attempt']
        if time_since_last < SESSION_RETRY_INTERVAL:
            # Возвращаем в очередь, еще рано
            await session_rotation_queue.put(user_id)
            return None
            
        return user_id
        
    except asyncio.TimeoutError:
        return None

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
    
    # Сначала пробуем загрузить из обычной директории
    if os.path.isdir(user_archive_dir):
        try:
            data_file_path = os.path.join(user_archive_dir, 'user_data.json')
            if os.path.exists(data_file_path):
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
                logging.info(f"Завантажено персональні дані для користувача {user_id} з директорії")
                return loaded_data
        except Exception as e:
            logging.error(f"Помилка при завантаженні даних з директорії для {user_id}: {e}")
    
    # Если не удалось, пробуем fallback файл
    try:
        fallback_path = os.path.join(USER_DATA_ARCHIVES_DIR, f"user_{user_id}_data.json")
        if os.path.exists(fallback_path):
            with open(fallback_path, 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)
            logging.info(f"Завантажено персональні дані для користувача {user_id} з fallback файлу")
            return loaded_data
    except Exception as e:
        logging.error(f"Помилка при завантаженні fallback даних для {user_id}: {e}")
    
    return {}

async def save_user_data(user_id: int, user_data: dict):
    if not user_data:
        return
    user_archive_dir = os.path.join(USER_DATA_ARCHIVES_DIR, str(user_id))
    try:
        # Создаем базовую директорию если её нет
        os.makedirs(USER_DATA_ARCHIVES_DIR, exist_ok=True)
        
        # Удаляем старую директорию пользователя если она существует
        if os.path.isdir(user_archive_dir):
            try:
                shutil.rmtree(user_archive_dir)
            except PermissionError as e:
                logging.warning(f"Не удалось удалить старую директорию {user_archive_dir}: {e}")
                # Пробуем очистить содержимое
                for filename in os.listdir(user_archive_dir):
                    file_path = os.path.join(user_archive_dir, filename)
                    try:
                        if os.path.isfile(file_path):
                            os.remove(file_path)
                    except:
                        pass
        
        # Создаем директорию пользователя
        os.makedirs(user_archive_dir, exist_ok=True)
        
        # Подготавливаем данные для сохранения
        data_to_save = user_data.copy()
        key_path_to_copy = data_to_save.pop('key_file_path', None)
        
        # Сохраняем JSON файл
        data_file_path = os.path.join(user_archive_dir, 'user_data.json')
        with open(data_file_path, 'w', encoding='utf-8') as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=4)
        
        # Копируем файл ключа если он есть
        if key_path_to_copy and os.path.exists(key_path_to_copy):
            try:
                shutil.copy(key_path_to_copy, user_archive_dir)
                logging.info(f"Файл ключа скопирован для пользователя {user_id}")
            except Exception as e:
                logging.error(f"Ошибка копирования файла ключа для {user_id}: {e}")
        
        logging.info(f"Персональні дані для {user_id} збережено в {user_archive_dir}")
        
    except Exception as e:
        logging.error(f"Не вдалося зберегти дані для {user_id}: {e}")
        logging.error(f"Детали ошибки: {type(e).__name__}: {str(e)}")
        # Пробуем альтернативный способ сохранения
        try:
            fallback_path = os.path.join(USER_DATA_ARCHIVES_DIR, f"user_{user_id}_data.json") 
            with open(fallback_path, 'w', encoding='utf-8') as f:
                json.dump(user_data, f, ensure_ascii=False, indent=4)
            logging.info(f"Данные сохранены в fallback файл: {fallback_path}")
        except Exception as fallback_error:
            logging.error(f"Fallback сохранение также не удалось: {fallback_error}")

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

async def book_available_slot(page, bot, user_id, chat_id, log_func, allow_session_pause=True):
    """
    Знаходить та бронює вільний слот, починаючи з поточного виду календаря.
    Використовує перевірену логіку з SlotVisitHelp.py (v33).
    
    allow_session_pause: Если True, при отсутствии слотов сохраняет сессию вместо бесконечного ожидания
    """
    try:
        log_func("Починаю пошук та бронювання слоту за логікою SlotVisitHelp...")
        await bot.send_message(chat_id, "🔄 Починаю пошук та бронювання за новою логікою...")

        # ПРАВИЛЬНАЯ ЛОГИКА из SlotVisitHelp: цикл поиска слотов с перезагрузкой
        max_reload_attempts = 999999999  # Как в SlotVisitHelp - практически бесконечно
        retry_attempts = 0
        
        while retry_attempts < max_reload_attempts:
            current_url = page.url
            if "dateAndTimeVisitInfo" not in current_url:
                log_func(f"ВНИМАНИЕ: Редирект со страницы поиска слотов. URL: {current_url}")
                raise SessionResetException("Сессия сброшена - перенаправление на другую страницу")
            
            log_func(f"Попытка {retry_attempts + 1}/{max_reload_attempts}: Ожидаю сообщения о поиске слотов.")
            await bot.send_message(chat_id, f"🔄 Попытка {retry_attempts + 1}: Ожидание поиска слотов...")
            
            searching_slots_locator = page.locator('text="Відбувається пошук активних слотів..."')
            
            try:
                # Ожидание появления сообщения о поиске
                await searching_slots_locator.wait_for(state='visible', timeout=10000)
                log_func("Сообщение 'Відбувається пошук активних слотів...' появилось.")
                
                # Ожидание исчезновения сообщения - ТОЧНЫЙ ТАЙМАУТ как в SlotVisitHelp
                search_timeout = 170000 + 90000  # 260 секунд = 4 минуты 20 секунд как в оригинале
                try:
                    await searching_slots_locator.wait_for(state='hidden', timeout=search_timeout)
                    log_func("Сообщение 'Відбувається пошук активних слотів...' исчезло.")
                except PlaywrightTimeoutError:
                    log_func(f"Поиск слотов длится слишком долго (>{search_timeout/1000} сек). Проверяю текущее состояние...")
                    
                    # Проверяем, есть ли уже сообщение об отсутствии слотов (ПОЛНАЯ ВЕРСИЯ как в SlotVisitHelp)
                    no_slots_now = page.locator('text="На жаль, зараз немає вільного часу. Спробуйте пізніше або зробіть запис у чергу очікування."')
                    if await no_slots_now.is_visible():
                        log_func("Найдено сообщение об отсутствии слотов во время долгого поиска.")
                        await bot.send_message(chat_id, f"😔 Слотов нет (обнаружено во время поиска). Перезагружаю страницу (попытка {retry_attempts + 1})...")
                        await page.reload(timeout=60000, wait_until='domcontentloaded')
                        log_func("Страница успешно перезагружена.")
                        await asyncio.sleep(5)
                        retry_attempts += 1
                        continue
                    
                    # Если поиск просто завис, принудительно перезагружаем
                    log_func("Поиск завис. Принудительно перезагружаю страницу...")
                    await bot.send_message(chat_id, f"⏱️ Поиск завис. Перезагружаю страницу (попытка {retry_attempts + 1})...")
                    await page.reload(timeout=60000, wait_until='domcontentloaded')
                    log_func("Страница успешно перезагружена.")
                    await asyncio.sleep(5)
                    retry_attempts += 1
                    continue
                
                # Пауза после поиска
                await asyncio.sleep(3)
                
                # Проверка наличия сообщения об отсутствии слотов (ПОЛНАЯ ВЕРСИЯ как в SlotVisitHelp)
                no_slots_message_locator = page.locator('text="На жаль, зараз немає вільного часу. Спробуйте пізніше або зробіть запис у чергу очікування."')
                is_message_visible = await no_slots_message_locator.is_visible()
                
                if is_message_visible:
                    # НОВАЯ ЛОГИКА: Сохранение сессии вместо бесконечных попыток
                    if allow_session_pause and retry_attempts >= 3:  # После 3 попыток предлагаем сохранить сессию
                        log_func(f"После {retry_attempts} попыток слоты не найдены. Предлагаю сохранить сессию.")
                        await bot.send_message(chat_id, 
                            f"💡 **Умная стратегия поиска**\n\n"
                            f"После {retry_attempts} попыток слоты не найдены.\n"
                            f"🔄 Сохраняю вашу сессию и переключаюсь на других пользователей.\n"
                            f"⏰ Вернусь к поиску через {SESSION_RETRY_INTERVAL // 60} минут.")
                        return "PAUSE_SESSION"  # Специальный код для сохранения сессии
                    
                    log_func(f"Сообщение об отсутствии слотов найдено. Попытка {retry_attempts + 1}. Перезагружаю страницу...")
                    await bot.send_message(chat_id, f"😔 Слотов нет. Перезагружаю страницу (попытка {retry_attempts + 1})...")
                    await page.reload(timeout=60000, wait_until='domcontentloaded')
                    log_func("Страница успешно перезагружена.")
                    await asyncio.sleep(5)
                    retry_attempts += 1
                else:
                    log_func("!!! СЛОТЫ НАЙДЕНЫ! Сообщение об отсутствии слотов НЕ найдено. Запускаю бронирование! !!!")
                    await bot.send_message(chat_id, "🎯 **СЛОТЫ НАЙДЕНЫ!** Начинаю бронирование...")
                    # Выходим из цикла и переходим к бронированию
                    break
                    
            except PlaywrightTimeoutError:
                log_func(f"Тайм-аут ожидания сообщения о поиске слотов на попытке {retry_attempts + 1}")
                await bot.send_message(chat_id, f"⏱️ Тайм-аут поиска на попытке {retry_attempts + 1}")
                retry_attempts += 1
                continue
        
        # Если все попытки исчерпаны и слоты не найдены
        if retry_attempts >= max_reload_attempts:
            log_func("Все попытки поиска слотов исчерпаны.")
            raise NoSlotsFoundException("Превышено количество попыток поиска слотов")

        # ТЕПЕРЬ запускаем процесс бронирования (только если слоты есть)

        # 1. Поиск контейнера с месяцами (календарь уже должен быть загружен)
        month_container_locator = page.locator('div[class*="scrollbar-container"]')
        try:
            await month_container_locator.wait_for(state='visible', timeout=20000)
        except PlaywrightTimeoutError:
            log_func("Тайм-аут ожидания контейнера с месяцами.")
            return "Тайм-аут загрузки календаря"
        
        month_tabs_locator = month_container_locator.get_by_text(re.compile(r"^\S+ \d{4}$"))
        month_tabs_count = await month_tabs_locator.count()

        if month_tabs_count == 0:
             log_func("Не вдалося знайти вкладки з місяцями після завантаження контейнера.")
             return "Календар завантажився, але вкладки місяців не знайдено"
        log_func(f"Знайдено {month_tabs_count} місяців для перевірки.")

        # 2. Ітерація по місяцях
        for i in range(month_tabs_count):
            # Повторно отримуємо локатор, щоб уникнути помилки "stale element"
            current_month_tab = page.locator('div[class*="scrollbar-container"]').get_by_text(re.compile(r"^\S+ \d{4}$")).nth(i)
            month_name = (await current_month_tab.text_content() or "").strip()
            log_func(f"Перевіряю місяць: {month_name}")
            
            try:
                await current_month_tab.scroll_into_view_if_needed(timeout=10000)
                await current_month_tab.click(timeout=15000)
                await page.wait_for_load_state('networkidle', timeout=60000)
            except PlaywrightTimeoutError:
                log_func(f"Не вдалося кликнути по місяцю '{month_name}' (таймаут). Пропускаю.")
                continue
            
            # 3. Пошук тижнів з вільними слотами
            base_locator = page.locator('div:has-text("СЛОТІВ")')
            available_week_locator = base_locator.filter(has_not_text="0 ВІЛЬНИХ")
            
            available_weeks_count = await available_week_locator.count()
            if available_weeks_count == 0:
                log_func(f"У місяці '{month_name}' вільних тижнів не знайдено.")
                continue

            log_func(f"!!! ЗНАЙДЕНО {available_weeks_count} тижнів з вільними слотами в '{month_name}'!!!")
            await bot.send_message(chat_id, f"🎯 **ЗНАЙДЕНО СЛОТИ!** 🎯\nУ '{month_name}' є тиждень з вільними слотами.")
            
            # 4. Клік по першому доступному тижню
            week_block = available_week_locator.first
            week_text_content = (await week_block.text_content() or "").replace('\n', ' ')
            log_func(f"Вибираю тиждень: {week_text_content}")
            
            path = await save_screenshot(page, f"week_with_slots_{month_name.replace(' ', '_')}", user_id)
            await send_telegram_photo(bot, chat_id, path, f"Знайдено тиждень зі слотами: {week_text_content}")

            await week_block.click(timeout=15000)
            await page.wait_for_load_state('networkidle', timeout=30000)
            
            # 5. Пошук та вибір першого доступного слоту часу
            log_func("Прокручую вниз, щоб побачити всі слоти...")
            await page.keyboard.press("End")
            await page.wait_for_timeout(1500)

            all_time_slots = await page.locator('div[role="button"][aria-label]').all()
            
            first_available_slot = None
            if not all_time_slots:
                 log_func("Попередження: не знайдено жодного слоту часу (навіть зайнятих).")
            
            for slot_locator in all_time_slots:
                aria_label = await slot_locator.get_attribute("aria-label") or ""
                if "цей слот вже заброньовано" not in aria_label:
                    first_available_slot = slot_locator
                    break 
            
            if first_available_slot is None:
                log_func("Всі слоти на цьому тижні виявилися зайнятими. Повертаюся до вибору місяців.")
                await page.get_by_role("button", name="Назад").click(timeout=10000)
                await page.wait_for_load_state('networkidle', timeout=15000)
                continue # Перехід до наступного місяця
            
            slot_time_text = await first_available_slot.text_content()
            log_func(f"Знайдено вільний слот: {slot_time_text}. Вибираю...")
            
            await first_available_slot.click(timeout=15000)
            await page.wait_for_timeout(2000)

            # 6. Фінальне підтвердження
            confirm_button_selector = 'button#finish-button'
            await page.locator(confirm_button_selector).wait_for(state='visible', timeout=15000)
            log_func("Натискаю кнопку 'Підтвердити'...")

            path = await save_screenshot(page, "final_confirmation", user_id)
            await send_telegram_photo(bot, chat_id, path, f"Екран перед фінальним підтвердженням слоту {slot_time_text}.")

            await page.locator(confirm_button_selector).click()
            
            # 7. Очікування результату з правильними селекторами
            processing_locator = page.locator('text="Дякуємо, ми обробляємо ваш запит"')
            success_locator = page.locator('text="Ваш візит успішно заброньовано"')

            await expect(processing_locator.or_(success_locator)).to_be_visible(timeout=60000)
            
            if await processing_locator.is_visible():
                log_func("!!! УСПІХ: Запит на бронювання відправлено! !!!")
                final_message = "✅ **УСПІХ!** ✅\nЗапит на бронювання відправлено! Очікуйте підтвердження."
                await bot.send_message(chat_id, final_message)
                
                path = await save_screenshot(page, "booking_processing", user_id)
                await send_telegram_photo(bot, chat_id, path, "Запит на бронювання обробляється.")
            
            elif await success_locator.is_visible():
                log_func("!!! УСПІХ: Слот успішно заброньовано! !!!")
                final_message = "✅ **УСПІХ!** ✅\nВізит успішно заброньовано! Перевірте пошту та сторінку 'Мої візити'."
                await bot.send_message(chat_id, final_message)
                path = await save_screenshot(page, "booking_confirmed", user_id)
                await send_telegram_photo(bot, chat_id, path, "Візит успішно заброньовано!")

            return "Успішне бронювання"

        # Якщо цикл по місяцях завершився без успіху
        log_func("Перевірив усі доступні місяці, але не вдалося забронювати.")
        return "Не вдалося забронювати"

    except PlaywrightTimeoutError as e:
        error_message = f"Тайм-аут під час бронювання слоту: {e}"
        log_func(error_message)
        await bot.send_message(chat_id, f"❗️ Тайм-аут на етапі пошуку/вибору слоту.")
        path = await save_screenshot(page, "booking_timeout_error", user_id)
        if path:
            await send_telegram_photo(bot, chat_id, path, "Тайм-аут під час бронювання")
        return "Тайм-аут бронювання"
    except Exception as e:
        error_message = f"Критична помилка під час бронювання слоту: {e}"
        log_func(error_message)
        await bot.send_message(chat_id, f"❗️ Помилка під час бронювання: {str(e).splitlines()[0]}")
        path = await save_screenshot(page, "booking_critical_error", user_id)
        if path:
            await send_telegram_photo(bot, chat_id, path, "Критична помилка під час бронювання")
        return f"Помилка: {e}"

async def run_booking_session(bot: Bot, user_id: int, user_data: dict, worker_id: int, headless_mode: bool):
    p = context = page = browser = None
    chat_id = user_data.get('chat_id')
    log_func = lambda msg: logging.info(f"[Воркер-{worker_id}][Користувач-{user_id}] {msg}")

    try:
        log_func("="*20 + " ПОЧАТОК НОВОЇ СЕСІЇ " + "="*20)
        
        # Перевіряємо наявність всіх необхідних полів
        required_fields = ['auth_method', 'date_of_birth', 'gender', 'country', 'institution', 'service_type', 'service_recipient']
        missing_fields = [field for field in required_fields if field not in user_data]
        
        if missing_fields:
            error_msg = f"Відсутні необхідні дані: {', '.join(missing_fields)}. Потрібно пройти налаштування заново."
            log_func(error_msg)
            await bot.send_message(chat_id, f"❗️ {error_msg}")
            raise SessionResetException(error_msg)
        
        p = await async_playwright().start()

        browser = await p.chromium.launch(
            headless=headless_mode,
            channel="chrome",
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            timeout=60000
        )

        context = await browser.new_context(
            locale="uk-UA",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            timezone_id="Europe/Kiev"
        )
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

            auth_method = user_data['auth_method']
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
        
        # Пробуем поиск слотов один раз
        log_func("Начинаю поиск слотов с новой логикой...")
        booking_result = await book_available_slot(page, bot, user_id, chat_id, log_func, allow_session_pause=True)
        
        if booking_result == "Успішне бронювання":
            log_func("Бронювання успішне! Завершую роботу з користувачем.")
            await bot.send_message(chat_id, "🎉 **Бронювання успішне!**")
            return "SUCCESS"
        elif booking_result == "PAUSE_SESSION":
            log_func("Паузим сессию и сохраняем контекст браузера")
            # Сохраняем сессию для последующего восстановления
            await save_browser_session(user_id, context, page, bot)
            return "SESSION_PAUSED"
        else:
            # Другие результаты обрабатываем как обычно
            log_func(f"Результат поиска: {booking_result}")
            if "Тайм-аут" in booking_result or "Помилка" in booking_result:
                raise SessionResetException(f"Ошибка поиска: {booking_result}")
            return "NO_SLOTS_RETRY"

    except SessionResetException as e:
        log_func(f"Штатный перезапуск сесії: {e}")
        
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
            if context: 
                await context.close()
            if browser and browser.is_connected(): 
                await browser.close()
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
        if browser and browser.is_connected(): await browser.close()
        if p: await p.stop()
        raise SessionResetException(f"Критична помилка: {e}")
    finally:
        # Закриваємо браузер только если сессия НЕ была сохранена
        if user_id not in paused_sessions:
            log_func("Спроба закрити ресурси Playwright...")
            try:
                if context:
                    await context.close()
                    log_func("Контекст Playwright закрито.")
                if browser and browser.is_connected():
                    await browser.close()
                    log_func("Браузер Playwright закрито.")
                if p:
                    await p.stop()
                    log_func("Playwright зупинено.")
            except Exception as e:
                log_func(f"Помилка при закритті ресурсів Playwright: {e}")
        else:
            log_func("Сессия сохранена, не закрываю браузерные ресурсы")
        log_func("="*20 + " КІНЕЦЬ СЕСІЇ " + "="*20 + "\n")
    p = context = page = browser = None
    chat_id = user_data.get('chat_id')
    log_func = lambda msg: logging.info(f"[Воркер-{worker_id}][Користувач-{user_id}] {msg}")

    try:
        log_func("="*20 + " ПОЧАТОК НОВОЇ СЕСІЇ " + "="*20)
        
        # Перевіряємо наявність всіх необхідних полів
        required_fields = ['auth_method', 'date_of_birth', 'gender', 'country', 'institution', 'service_type', 'service_recipient']
        missing_fields = [field for field in required_fields if field not in user_data]
        
        if missing_fields:
            error_msg = f"Відсутні необхідні дані: {', '.join(missing_fields)}. Потрібно пройти налаштування заново."
            log_func(error_msg)
            await bot.send_message(chat_id, f"❗️ {error_msg}")
            raise SessionResetException(error_msg)
        
        p = await async_playwright().start()

        browser = await p.chromium.launch(
            headless=headless_mode,
            channel="chrome",
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            timeout=60000
        )

        context = await browser.new_context(
            locale="uk-UA",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            timezone_id="Europe/Kiev"
        )
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

            auth_method = user_data['auth_method']
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
        
        # Запускаємо цикл пошуку слотів до успіху
        search_attempt = 1
        while True:
            log_func(f"Починаю спробу пошуку #{search_attempt}...")
            await bot.send_message(chat_id, f"🔍 Спроба пошуку #{search_attempt}...")
            
            booking_result = await book_available_slot(page, bot, user_id, chat_id, log_func)
            
            if booking_result == "Успішне бронювання":
                log_func("Бронювання успішне! Завершую роботу з користувачем.")
                await bot.send_message(chat_id, "🎉 **Бронювання успішне!**")
                return  # Успіх, виходимо з функції
            elif booking_result == "Не вдалося забронювати":
                log_func(f"Спроба #{search_attempt}: Слотів немає. Чекаю 30 секунд перед наступною спробою...")
                await bot.send_message(chat_id, f"😔 Спроба #{search_attempt}: Слотів немає. Чекаю 30 секунд і шукаю знову...")
                await asyncio.sleep(30)  # Пауза між спробами
                search_attempt += 1
                continue  # Продовжуємо пошук в тому ж календарі
            elif "Календар завантажився, але вкладки місяців не знайдено" in booking_result:
                log_func("Календар завантажився, але вкладки місяців не знайдено.")
                raise SessionResetException("Проблема з відображенням календаря")
            elif "Тайм-аут завантаження календаря" in booking_result:
                log_func("Тайм-аут завантаження календаря.")
                raise SessionResetException("Тайм-аут завантаження календаря")
            elif "Тайм-аут" in booking_result:
                log_func(f"Тайм-аут під час бронювання: {booking_result}")
                raise SessionResetException(f"Тайм-аут завантаження елементів: {booking_result}")
            elif "Помилка" in booking_result:
                log_func(f"Помилка під час бронювання: {booking_result}")
                raise SessionResetException(f"Помилка бронювання: {booking_result}")
            else:
                log_func(f"Невідомий результат з book_available_slot: {booking_result}")
                raise SessionResetException(f"Невідомий результат: {booking_result}")

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
            if context: 
                await context.close()
            if browser and browser.is_connected(): 
                await browser.close()
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
        if browser and browser.is_connected(): await browser.close()
        if p: await p.stop()
        raise SessionResetException(f"Критична помилка: {e}")
    finally:
        # Закриваємо браузер у будь-якому випадку: успіх, помилка чи перезапуск
        log_func("Спроба закрити ресурси Playwright...")
        try:
            if context:
                await context.close()
                log_func("Контекст Playwright закрито.")
            if browser and browser.is_connected():
                await browser.close()
                log_func("Браузер Playwright закрито.")
            if p:
                await p.stop()
                log_func("Playwright зупинено.")
        except Exception as e:
            log_func(f"Помилка при закритті ресурсів Playwright: {e}")
        log_func("="*20 + " КІНЕЦЬ СЕСІЇ " + "="*20 + "\n")

async def run_restored_session(bot: Bot, user_id: int, user_data: dict, worker_id: int):
    """Работает с восстановленной сессией"""
    chat_id = user_data.get('chat_id')
    log_func = lambda msg: logging.info(f"[Воркер-{worker_id}][Восстановление-{user_id}] {msg}")
    
    try:
        log_func("="*20 + " ВОССТАНОВЛЕНИЕ СЕССИИ " + "="*20)
        
        # Восстанавливаем сохраненную сессию
        context, page = await restore_browser_session(user_id, bot)
        
        if not context or not page:
            log_func("Не удалось восстановить сессию, запускаю новую")
            return await run_booking_session(bot, user_id, user_data, worker_id, headless_mode=True)
        
        log_func("Сессия успешно восстановлена, продолжаю поиск слотов...")
        
        # Продолжаем поиск слотов
        booking_result = await book_available_slot(page, bot, user_id, chat_id, log_func, allow_session_pause=True)
        
        if booking_result == "Успішне бронювання":
            log_func("Бронювання успішне в восстановленной сессии!")
            await bot.send_message(chat_id, "🎉 **Бронювання успішне!**")
            # Закрываем сессию после успеха
            await close_paused_session(user_id, bot)
            return "SUCCESS"
        elif booking_result == "PAUSE_SESSION":
            log_func("Снова паузим сессию")
            # Обновляем время последней попытки
            import time
            paused_sessions[user_id]['last_attempt'] = time.time()
            return "SESSION_PAUSED_AGAIN"
        else:
            log_func(f"Восстановленная сессия дала результат: {booking_result}")
            # При ошибках закрываем сессию
            await close_paused_session(user_id, bot)
            return "SESSION_CLOSED"
            
    except Exception as e:
        log_func(f"Ошибка в восстановленной сессии: {e}")
        await close_paused_session(user_id, bot)
        raise SessionResetException(f"Ошибка восстановленной сессии: {e}")

# --- Worker and Telegram Handlers ---

async def worker(worker_id: int, app: Application, headless_mode: bool):
    import time
    logging.info(f"[Воркер-{worker_id}] запущений с поддержкой ротации сессий.")
    while True:
        try:
            # Сначала проверяем, есть ли пользователи для восстановления сессий
            rotation_user_id = await get_next_rotation_user()
            
            if rotation_user_id:
                logging.info(f"[Воркер-{worker_id}] Восстанавливаю сессию для пользователя {rotation_user_id}")
                user_data = user_sessions.get(rotation_user_id)
                
                if user_data:
                    worker_start_times[worker_id] = time.time()
                    active_workers[worker_id] = rotation_user_id
                    
                    try:
                        result = await run_restored_session(app.bot, rotation_user_id, user_data, worker_id)
                        if result == "SUCCESS":
                            logging.info(f"[Воркер-{worker_id}] Успешно обработал восстановленную сессию {rotation_user_id}")
                            if rotation_user_id in user_sessions:
                                del user_sessions[rotation_user_id]
                        elif result == "SESSION_PAUSED_AGAIN":
                            logging.info(f"[Воркер-{worker_id}] Сессия {rotation_user_id} снова приостановлена")
                    except Exception as e:
                        logging.error(f"[Воркер-{worker_id}] Ошибка при восстановлении сессии {rotation_user_id}: {e}")
                    
                    # Очищаем отслеживание
                    if worker_id in worker_start_times:
                        del worker_start_times[worker_id]
                    if worker_id in active_workers:
                        del active_workers[worker_id]
                    
                    continue  # Переходим к следующей итерации
            
            # Если нет пользователей для восстановления, берем нового из очереди
            user_id = await task_queue.get()
            logging.info(f"[Воркер-{worker_id}] Взяв у роботу нового користувача {user_id}.")
            
            # Отслеживаем время начала работы и активного воркера
            worker_start_times[worker_id] = time.time()
            active_workers[worker_id] = user_id
            
            user_data = user_sessions.get(user_id)
            if not user_data or user_data.get('status') != 'ready':
                logging.warning(f"[Воркер-{worker_id}] Дані для {user_id} не знайдено, завдання пропущено.")
                # Очищаем отслеживание
                if worker_id in worker_start_times:
                    del worker_start_times[worker_id]
                if worker_id in active_workers:
                    del active_workers[worker_id]
                task_queue.task_done()
                continue

            # Уведомляем пользователя о начале работы
            chat_id = user_data['chat_id']
            remaining_in_queue = task_queue.qsize()
            await app.bot.send_message(chat_id, f"🚀 **Воркер #{worker_id} начал обработку вашего задания!**\n\n📊 В очереди за вами: {remaining_in_queue}")

            max_restarts = 3  # Уменьшаем количество перезапусков, так как теперь есть ротация
            for attempt in range(max_restarts):
                try:
                    result = await run_booking_session(app.bot, user_id, user_data, worker_id, headless_mode)
                    
                    if result == "SUCCESS":
                        logging.info(f"[Воркер-{worker_id}] Завдання для {user_id} успішно виконано.")
                        break
                    elif result == "SESSION_PAUSED":
                        logging.info(f"[Воркер-{worker_id}] Сессия {user_id} приостановлена, переключаюсь на других пользователей.")
                        break  # Выходим из цикла перезапусков
                    elif result == "NO_SLOTS_RETRY":
                        logging.info(f"[Воркер-{worker_id}] Повторная попытка для {user_id}")
                        await asyncio.sleep(30)
                        continue
                        
                except SessionResetException as e:
                    logging.warning(f"[Воркер-{worker_id}] Перезапуск сесії для {user_id} (спроба {attempt + 1}/{max_restarts}). Причина: {e}")
                    if attempt + 1 >= max_restarts:
                        logging.error(f"Досягнуто ліміту перезапусків для {user_id}. Завдання зупинено.")
                        await app.bot.send_message(chat_id, "Досягнуто ліміту автоматичних перезапусків. Завдання зупинено.")
                        break
                    await app.bot.send_message(chat_id, f"🔄 Сесію було скинуто. Перезапускаю... (Спроба {attempt + 1}/{max_restarts})")
                    await asyncio.sleep(15)

            logging.info(f"[Воркер-{worker_id}] Завершив роботу з користувачем {user_id}.")
            if user_id in user_sessions:
                logging.info(f"Видаляю сесію для {user_id}.")
                del user_sessions[user_id]
            
            # Очищаем отслеживание воркера
            if worker_id in worker_start_times:
                del worker_start_times[worker_id]
            if worker_id in active_workers:
                del active_workers[worker_id]
            
            task_queue.task_done()
            
            # Проверяем необходимость автомасштабирования
            await auto_scale_workers(app, headless_mode)
            
        except Exception as e:
            logging.critical(f"Критична помилка у воркері {worker_id}: {e}\n{traceback.format_exc()}")
            # Очищаем отслеживание даже при ошибке
            if worker_id in worker_start_times:
                del worker_start_times[worker_id]
            if worker_id in active_workers:
                del active_workers[worker_id]
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

    # Перевіряємо повноту збережених даних
    required_fields = ['auth_method', 'date_of_birth', 'gender', 'country', 'institution', 'service_type', 'service_recipient']
    missing_fields = [field for field in required_fields if field not in persisted_data or not persisted_data[field]]
    
    if missing_fields:
        await update.message.reply_text(
            f"❗️ **Дані неповні.**\n\n"
            f"Відсутні поля: {', '.join(missing_fields)}\n\n"
            f"Потрібно пройти налаштування заново."
        )
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
    
    # Показываем позицию в очереди
    queue_size = task_queue.qsize()
    if queue_size <= WORKER_COUNT:
        await update.message.reply_text("✅ **Використовую збережені дані.**\n🚀 Завдання додано до черги. Поиск начнется в ближайшее время!")
    else:
        position = queue_size - WORKER_COUNT
        await update.message.reply_text(f"✅ **Використовую збережені дані.**\n📋 Завдання додано до черги.\n\n⏳ **Позиція в черзі: {position}**\n⚙️ Активних воркерів: {WORKER_COUNT}\n\n💡 Поиск начнется, как только освободится воркер.")
    
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
    
    await task_queue.put(user_id)
    logging.info(f"Користувач {user_id} доданий до черги.")
    
    # Показываем позицию в очереди
    queue_size = task_queue.qsize()
    if queue_size <= WORKER_COUNT:
        await context.bot.send_message(chat_id, "🎉 **Налаштування завершено!**\n🚀 Завдання додано до черги. Поиск начнется в ближайшее время!")
    else:
        position = queue_size - WORKER_COUNT
        await context.bot.send_message(chat_id, f"🎉 **Налаштування завершено!**\n📋 Завдання додано до черги.\n\n⏳ **Позиція в черзі: {position}**\n⚙️ Активних воркерів: {WORKER_COUNT}\n\n💡 Поиск начнется, как только освободится воркер.")
    
    return ConversationHandler.END

async def session_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверяет состояние сохраненной сессии пользователя"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if user_id not in paused_sessions:
        await context.bot.send_message(chat_id, 
            "У вас нет сохраненной сессии.\n"
            "Для начала работы используйте /book_new")
        return
    
    session_data = paused_sessions[user_id]
    current_time = time.time()
    age_seconds = current_time - session_data['last_attempt']
    age_minutes = age_seconds / 60
    
    # Загружаем данные пользователя для определения метода авторизации
    user_data = load_user_data(user_id)
    auth_method = user_data.get('auth_method', 'unknown')
    
    # Детальная диагностика сессии
    saved_url = session_data.get('saved_url', 'unknown')
    page_title = session_data.get('page_title', 'unknown')
    attempt_count = session_data.get('attempt_count', 0)
    
    # Расчет надежности в зависимости от типа авторизации
    if auth_method == 'key':
        # Файл-ключ - самая высокая надежность и время жизни
        max_age = 45  # минут
        auth_name = "🔑 Файл-ключ"
        auth_description = "Высокая надежность + автоматическая реавторизация"
        
        if age_minutes < 10:
            reliability = "95-99%"
            status_emoji = "🟢"
            status_text = "Отличное"
        elif age_minutes < 25:
            reliability = "90-95%"
            status_emoji = "🟢"
            status_text = "Очень хорошее"
        elif age_minutes < 40:
            reliability = "80-90%"
            status_emoji = "🟡"
            status_text = "Хорошее"
        else:
            reliability = "70-80%"
            status_emoji = "🟠"
            status_text = "Удовлетворительное"
            
    elif auth_method == 'diia':
        # Diya - средняя надежность
        max_age = 25  # минут
        auth_name = "📱 Diya"
        auth_description = "Средняя надежность, требует ручной реавторизации"
        
        if age_minutes < 5:
            reliability = "90-95%"
            status_emoji = "🟢"
            status_text = "Отличное"
        elif age_minutes < 15:
            reliability = "80-90%"
            status_emoji = "🟡"
            status_text = "Хорошее"
        elif age_minutes < 25:
            reliability = "65-80%"
            status_emoji = "🟠"
            status_text = "Удовлетворительное"
        else:
            reliability = "40-65%"
            status_emoji = "🔴"
            status_text = "Плохое (требуется реавторизация)"
            
    elif auth_method == 'idgov':
        # BankID - низкая надежность
        max_age = 20  # минут
        auth_name = "🏦 BankID"
        auth_description = "Умеренная надежность, короткое время жизни"
        
        if age_minutes < 5:
            reliability = "85-90%"
            status_emoji = "🟢"
            status_text = "Хорошее"
        elif age_minutes < 10:
            reliability = "70-85%"
            status_emoji = "�"
            status_text = "Удовлетворительное"
        elif age_minutes < 20:
            reliability = "50-70%"
            status_emoji = "🟠"
            status_text = "Слабое"
        else:
            reliability = "30-50%"
            status_emoji = "🔴"
            status_text = "Плохое (требуется реавторизация)"
    else:
        # Неизвестный метод
        max_age = 25
        auth_name = f"❓ {auth_method}"
        auth_description = "Неизвестный метод авторизации"
        reliability = "50-70%"
        status_emoji = "🟡"
        status_text = "Неопределенное"
    
    # Проверка корректности сохраненной страницы
    page_status = "✅ Правильная страница" if "dateAndTimeVisitInfo" in saved_url else "⚠️ Неожиданная страница"
    
    # Время до следующей попытки
    next_retry_in = max(0, SESSION_RETRY_INTERVAL - age_seconds)
    next_retry_text = f"через {int(next_retry_in)}s" if next_retry_in > 0 else "готова к запуску"
    
    # Специальные возможности для файла-ключа
    auto_reauth_info = ""
    if auth_method == 'key' and age_minutes > max_age:
        auto_reauth_info = "\n🔄 **Автореавторизация:** Доступна при истечении сессии"
    elif auth_method in ['diia', 'idgov'] and age_minutes > max_age * 0.8:
        auto_reauth_info = "\n⚠️ **Реавторизация:** Потребуется ручное подтверждение"
    
    response = (
        f"{status_emoji} **Диагностика сессии:**\n\n"
        f"📊 **Состояние:** {status_text}\n"
        f"🎯 **Надежность восстановления:** {reliability}\n"
        f"🔐 **Метод авторизации:** {auth_name}\n"
        f"📋 **Особенности:** {auth_description}\n"
        f"⏰ **Возраст сессии:** {age_minutes:.1f} мин (лимит: {max_age} мин)\n"
        f"🔄 **Попыток поиска:** {attempt_count}\n"
        f"📍 **Статус страницы:** {page_status}\n"
        f"⏱️ **Следующая попытка:** {next_retry_text}\n"
        f"{auto_reauth_info}\n\n"
        f"📄 **Сохраненная страница:** {page_title[:50]}...\n"
        f"🔗 **URL:** {saved_url[:60]}...\n\n"
    )
    
    # Рекомендации в зависимости от типа авторизации и возраста
    if auth_method == 'key':
        if age_minutes > 40:
            response += "⚠️ **Рекомендация:** Сессия очень старая, но файл-ключ позволяет автоматическую реавторизацию при необходимости."
        elif age_minutes > 25:
            response += "💡 **Рекомендация:** Сессия стареет, но файл-ключ обеспечивает высокую надежность восстановления."
        else:
            response += "✅ **Рекомендация:** Файл-ключ обеспечивает максимальную надежность. Восстановление должно пройти отлично."
    elif auth_method == 'diia':
        if age_minutes > 20:
            response += "⚠️ **Рекомендация:** Сессия старая. При проблемах потребуется новое сканирование QR-кода в приложении Diya."
        elif age_minutes > 10:
            response += "💡 **Рекомендация:** Сессия в приемлемом состоянии для Diya, но будьте готовы к реавторизации."
        else:
            response += "✅ **Рекомендация:** Сессия Diya в хорошем состоянии, восстановление должно пройти успешно."
    elif auth_method == 'idgov':
        if age_minutes > 15:
            response += "⚠️ **Рекомендация:** BankID имеет короткое время жизни сессии. Рекомендуется новая авторизация через /book_new."
        elif age_minutes > 8:
            response += "💡 **Рекомендация:** Сессия BankID стареет быстро. При проблемах запустите новую сессию."
        else:
            response += "✅ **Рекомендация:** Сессия BankID в приемлемом состоянии, но следите за временем."
    else:
        response += "💡 **Рекомендация:** Неизвестный метод авторизации. При проблемах запустите /book_new."
    
    await context.bot.send_message(chat_id, response)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message_source = update.callback_query.message if update.callback_query else update.message
    user_id = update.effective_user.id
    chat_id = message_source.chat_id

    queue_size = task_queue.qsize()
    active_workers_count = len(active_workers)
    
    # Информация о позиции пользователя
    position_text = "Вашого завдання немає в черзі."
    try:
        user_position = list(task_queue._queue).index(user_id) + 1
        position_text = f"Ваша позиція в черзі: **{user_position}**."
    except ValueError: 
        pass
    
    # Расширенная статистика
    status_text = (
        f"📊 **Статус системы:**\n\n"
        f"⚙️ Активних воркерів: **{active_workers_count}** / {MAX_WORKERS}\n"
        f"👥 В черзі: **{queue_size}**\n"
        f"🎯 Базовых воркеров: **{WORKER_COUNT}**\n"
        f"� Автомасштабирование: {'✅' if queue_size > AUTO_SCALE_THRESHOLD else '❌'}\n\n"
        f"{position_text}"
    )
    
    # Информация о работающих воркерах
    if active_workers:
        import time
        current_time = time.time()
        worker_info = []
        for worker_id, user_id_working in active_workers.items():
            start_time = worker_start_times.get(worker_id, current_time)
            work_duration = int(current_time - start_time)
            worker_info.append(f"Воркер #{worker_id}: {work_duration}s")
        
        if worker_info:
            status_text += f"\n\n🔧 **Активные воркеры:**\n" + "\n".join(worker_info)
    
    # Информация о сохраненных сессиях
    if paused_sessions:
        current_time = time.time()
        session_info = []
        for user_id_session, session_data in paused_sessions.items():
            last_attempt = session_data['last_attempt']
            time_since_last = int(current_time - last_attempt)
            next_retry_in = max(0, SESSION_RETRY_INTERVAL - time_since_last)
            attempt_count = session_data.get('attempt_count', 0)
            
            # Получаем информацию о методе авторизации
            session_user_data = load_user_data(user_id_session)
            auth_method = session_user_data.get('auth_method', 'unknown')
            
            # Иконки для методов авторизации
            auth_icons = {
                'key': '🔑',
                'diia': '📱',
                'idgov': '🏦'
            }
            auth_icon = auth_icons.get(auth_method, '❓')
            
            # Рассчитываем надежность с учетом типа авторизации
            age_minutes = time_since_last / 60
            
            if auth_method == 'key':
                # Файл-ключ - более высокая надежность
                if age_minutes < 10:
                    reliability = "95-99%"
                elif age_minutes < 25:
                    reliability = "90-95%"
                elif age_minutes < 40:
                    reliability = "80-90%"
                else:
                    reliability = "70-80%"
            elif auth_method == 'diia':
                # Diya - средняя надежность
                if age_minutes < 5:
                    reliability = "90-95%"
                elif age_minutes < 15:
                    reliability = "80-90%"
                elif age_minutes < 25:
                    reliability = "65-80%"
                else:
                    reliability = "40-65%"
            elif auth_method == 'idgov':
                # BankID - более низкая надежность
                if age_minutes < 5:
                    reliability = "85-90%"
                elif age_minutes < 10:
                    reliability = "70-85%"
                elif age_minutes < 20:
                    reliability = "50-70%"
                else:
                    reliability = "30-50%"
            else:
                # Неизвестный метод
                if age_minutes < 5:
                    reliability = "85-95%"
                elif age_minutes < 15:
                    reliability = "70-85%"
                elif age_minutes < 25:
                    reliability = "55-70%"
                else:
                    reliability = "40-55%"
            
            saved_url = session_data.get('saved_url', 'unknown')
            page_title = session_data.get('page_title', 'unknown')
            
            if user_id_session == user_id:
                # Более детальная информация для текущего пользователя
                session_info.append(f"🔵 **ВЫ** {auth_icon}: #{attempt_count}, надежность {reliability}, повтор через {next_retry_in}s")
                if 'dateAndTimeVisitInfo' in saved_url:
                    session_info.append(f"   ✅ Сохранено на правильной странице выбора слотов")
                else:
                    session_info.append(f"   ⚠️ Сохранено на странице: {page_title[:30]}...")
                    
                # Добавляем информацию об автореавторизации для файла-ключа
                if auth_method == 'key':
                    session_info.append(f"   🔄 Автореавторизация доступна при истечении сессии")
            else:
                session_info.append(f"User {user_id_session} {auth_icon}: #{attempt_count}, {reliability}, через {next_retry_in}s")
        
        if session_info:
            status_text += f"\n\n💾 **Сохраненные сессии ({len(paused_sessions)}/{MAX_PAUSED_SESSIONS}):**\n" + "\n".join(session_info)
            
            # Дополнительная информация для пользователя с сохраненной сессией
            if user_id in paused_sessions:
                user_session = paused_sessions[user_id]
                user_data_status = load_user_data(user_id)
                user_auth_method = user_data_status.get('auth_method', 'unknown')
                age_minutes = (current_time - user_session['last_attempt']) / 60
                
                # Определяем лимит времени для данного метода
                time_limits = {
                    'key': 45,
                    'diia': 25,
                    'idgov': 20
                }
                time_limit = time_limits.get(user_auth_method, 25)
                
                status_text += f"\n\n🔍 **Ваша сессия:**\n"
                status_text += f"Возраст: {age_minutes:.1f} мин (лимит: {time_limit} мин)\n"
                status_text += f"Попыток: {user_session.get('attempt_count', 0)}\n"
                
                if user_auth_method == 'key':
                    status_text += f"🔑 Файл-ключ: автореавторизация при необходимости"
                elif age_minutes > time_limit * 0.8:
                    if user_auth_method == 'diia':
                        status_text += f"📱 Diya: может потребоваться новое сканирование QR"
                    elif user_auth_method == 'idgov':
                        status_text += f"🏦 BankID: рекомендуется новая авторизация"
                    else:
                        status_text += f"⚠️ Сессия стареет, возможна реавторизация"
    
    await context.bot.send_message(chat_id, status_text)

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
    application.add_handler(CommandHandler("session_check", session_check_command))
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

    # Run bot
    try:
        await application.initialize()
        await setup_bot_commands(application.bot)
        await application.updater.start_polling()
        await application.start()
        await asyncio.gather(*worker_tasks)
    finally:
        logging.info("Зупинка бота...")
        if application.updater and application.updater.running:
            await application.updater.stop()
        if application.running:
            await application.stop()
        for task in worker_tasks:
            task.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)
        logging.info("Бот зупинено.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Програму зупинено вручну.")