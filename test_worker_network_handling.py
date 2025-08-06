#!/usr/bin/env python3
"""
Тест обработки сетевых ошибок в функции worker()
"""

import asyncio
import logging
import sys
from unittest.mock import AsyncMock, MagicMock
from booking_bot import (
    is_network_error, 
    safe_send_message,
    NetworkConnectionException,
    TemporaryNetworkException
)

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class MockTelegramNetworkError(Exception):
    """Имитация telegram.error.NetworkError"""
    def __init__(self, message):
        super().__init__(message)
        self.__class__.__name__ = 'NetworkError'
        self.__module__ = 'telegram.error'

async def test_telegram_network_error_detection():
    """Тест определения telegram сетевых ошибок"""
    print("\n=== Тест определения Telegram сетевых ошибок ===")
    
    # Тестовые случаи сетевых ошибок
    network_errors = [
        MockTelegramNetworkError("httpx.ConnectError: [Errno 11001] getaddrinfo failed"),
        Exception("httpx.ConnectError: [Errno 11001] getaddrinfo failed"),
        Exception("Connection timeout"),
        Exception("Network is unreachable"),
        Exception("httpx.TimeoutError: Request timed out"),
        Exception("telegram.error.NetworkError: Connection lost")
    ]
    
    for i, error in enumerate(network_errors, 1):
        result = is_network_error(error)
        print(f"  {i}. {error} -> {'✅ СЕТЕВАЯ' if result else '❌ НЕ СЕТЕВАЯ'}")
        
        if not result:
            print(f"     ⚠️  Ошибка НЕ распознана как сетевая: {error}")

async def test_safe_send_message_with_network_errors():
    """Тест safe_send_message с сетевыми ошибками"""
    print("\n=== Тест safe_send_message с сетевыми ошибками ===")
    
    # Создаем мок бота
    mock_bot = MagicMock()
    
    # Тест 1: Telegram NetworkError
    print("1. Тест telegram.error.NetworkError:")
    mock_bot.send_message = AsyncMock(side_effect=MockTelegramNetworkError("httpx.ConnectError: [Errno 11001] getaddrinfo failed"))
    
    try:
        await safe_send_message(mock_bot, 123456, "Test message", "тест")
        print("   ❌ Ошибка: исключение не было поднято")
    except NetworkConnectionException as e:
        print(f"   ✅ Правильно поймана NetworkConnectionException: {e}")
    except Exception as e:
        print(f"   ⚠️  Поймано неожиданное исключение: {type(e).__name__}: {e}")
    
    # Тест 2: Обычная сетевая ошибка
    print("2. Тест обычной сетевой ошибки:")
    mock_bot.send_message = AsyncMock(side_effect=Exception("Connection timeout"))
    
    try:
        await safe_send_message(mock_bot, 123456, "Test message", "тест")
        print("   ❌ Ошибка: исключение не было поднято")
    except NetworkConnectionException as e:
        print(f"   ✅ Правильно поймана NetworkConnectionException: {e}")
    except Exception as e:
        print(f"   ⚠️  Поймано неожиданное исключение: {type(e).__name__}: {e}")
    
    # Тест 3: Успешная отправка
    print("3. Тест успешной отправки:")
    mock_bot.send_message = AsyncMock(return_value=True)
    
    try:
        await safe_send_message(mock_bot, 123456, "Test message", "тест")
        print("   ✅ Сообщение успешно отправлено")
    except Exception as e:
        print(f"   ❌ Неожиданная ошибка при успешной отправке: {e}")

async def test_network_error_classification():
    """Тест классификации различных типов ошибок"""
    print("\n=== Тест классификации ошибок ===")
    
    test_cases = [
        # Сетевые ошибки
        ("telegram.error.NetworkError: httpx.ConnectError: [Errno 11001] getaddrinfo failed", True),
        ("httpx.ConnectError: Connection refused", True),
        ("Connection timeout", True),
        ("Network is unreachable", True),
        ("DNS resolution failed", True),
        ("Host unreachable", True),
        
        # Не сетевые ошибки
        ("ValueError: Invalid input", False),
        ("KeyError: 'missing_key'", False),
        ("File not found", False),
        ("Permission denied", False),
    ]
    
    for error_msg, expected in test_cases:
        error = Exception(error_msg)
        result = is_network_error(error)
        status = "✅" if result == expected else "❌"
        print(f"  {status} '{error_msg}' -> {'Сетевая' if result else 'Не сетевая'}")

async def main():
    """Основная функция тестирования"""
    print("🔬 Тестирование обработки сетевых ошибок в worker()")
    print("=" * 60)
    
    try:
        await test_telegram_network_error_detection()
        await test_safe_send_message_with_network_errors()
        await test_network_error_classification()
        
        print("\n" + "=" * 60)
        print("✅ Все тесты завершены!")
        print("🔧 Теперь можно тестировать с реальным отключением WiFi")
        
    except Exception as e:
        print(f"\n❌ Ошибка при тестировании: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
