#!/usr/bin/env python3
"""
Тест системы устойчивости к сетевым ошибкам
"""

import asyncio
import logging
from booking_bot import (
    is_network_error, 
    is_temporary_error, 
    retry_network_operation,
    NetworkConnectionException,
    TemporaryNetworkException
)

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

async def test_network_error_detection():
    """Тестирует определение сетевых ошибок"""
    print("=== Тест определения сетевых ошибок ===")
    
    # Тестовые ошибки
    test_cases = [
        # Сетевые ошибки
        Exception("net::ERR_INTERNET_DISCONNECTED"),
        Exception("net::ERR_NETWORK_CHANGED"),
        Exception("net::ERR_DNS_FAILED"),
        Exception("connection refused"),
        Exception("timeout"),
        Exception("Network is unreachable"),
        
        # Временные ошибки
        Exception("502 Bad Gateway"),
        Exception("503 Service Unavailable"),
        Exception("504 Gateway Timeout"),
        Exception("Rate limit exceeded"),
        Exception("temporarily unavailable"),
        
        # Обычные ошибки
        Exception("Element not found"),
        Exception("Invalid selector"),
        Exception("Page not loaded"),
    ]
    
    for i, error in enumerate(test_cases):
        is_network = is_network_error(error)
        is_temp = is_temporary_error(error)
        print(f"Ошибка {i+1}: '{error}' -> Сетевая: {is_network}, Временная: {is_temp}")

async def test_retry_mechanism():
    """Тестирует механизм повторных попыток"""
    print("\n=== Тест механизма retry ===")
    
    # Счетчик попыток
    attempt_count = 0
    
    async def failing_operation():
        nonlocal attempt_count
        attempt_count += 1
        print(f"  Попытка {attempt_count}")
        
        if attempt_count < 3:
            # Используем сетевую ошибку, которая будет распознана системой
            raise Exception("timeout: Network request timed out")
        else:
            return "Успех!"
    
    try:
        result = await retry_network_operation(
            failing_operation,
            context_info="тестовая операция",
            max_attempts=3
        )
        print(f"Результат: {result}")
    except Exception as e:
        print(f"Ошибка: {e}")

async def test_network_exceptions():
    """Тестирует пользовательские исключения"""
    print("\n=== Тест пользовательских исключений ===")
    
    try:
        raise NetworkConnectionException("Тест сетевого исключения")
    except NetworkConnectionException as e:
        print(f"Поймано NetworkConnectionException: {e}")
    
    try:
        raise TemporaryNetworkException("Тест временного исключения")
    except TemporaryNetworkException as e:
        print(f"Поймано TemporaryNetworkException: {e}")

async def test_retry_with_real_timeout():
    """Тестирует retry с реальным тайм-аутом"""
    print("\n=== Тест retry с тайм-аутом ===")
    
    async def timeout_operation():
        print("  Начинаю операцию с тайм-аутом...")
        await asyncio.sleep(2)  # Симуляция медленной операции
        return "Операция завершена"
    
    try:
        # Тестируем с коротким тайм-аутом
        result = await asyncio.wait_for(
            retry_network_operation(
                timeout_operation,
                context_info="операция с тайм-аутом"
            ),
            timeout=1.0  # 1 секунда - должен вызвать тайм-аут
        )
        print(f"Результат: {result}")
    except asyncio.TimeoutError:
        print("Операция прервана по тайм-ауту (ожидается)")
    except Exception as e:
        print(f"Неожиданная ошибка: {e}")

async def main():
    """Запуск всех тестов"""
    print("Начинаю тестирование системы устойчивости к сетевым ошибкам...\n")
    
    await test_network_error_detection()
    await test_retry_mechanism()
    await test_network_exceptions()
    await test_retry_with_real_timeout()
    
    print("\n=== Тестирование завершено ===")

if __name__ == "__main__":
    asyncio.run(main())
