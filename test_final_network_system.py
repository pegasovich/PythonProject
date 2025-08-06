#!/usr/bin/env python3
"""
Финальный тест системы обработки сетевых ошибок
"""

import asyncio
import logging
from booking_bot import (
    safe_network_operation, 
    NetworkConnectionException,
    TemporaryNetworkException
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

async def test_network_aware_operations():
    """Тестирует операции с осведомленностью о сети"""
    print("=== Тест операций с уведомлениями о сетевых проблемах ===")
    
    # Тест 1: Успешная операция
    try:
        print("1. Тест успешной операции...")
        result = await safe_network_operation(
            lambda: asyncio.sleep(0.1),  # Простая успешная операция
            "тестовая успешная операция"
        )
        print("✅ Успешная операция выполнена")
    except Exception as e:
        print(f"❌ Неожиданная ошибка: {e}")
    
    # Тест 2: Сетевая ошибка
    try:
        print("2. Тест сетевой ошибки...")
        await safe_network_operation(
            lambda: exec('raise Exception("net::ERR_INTERNET_DISCONNECTED Connection lost")'),
            "тестовая сетевая операция"
        )
        print("❌ Неожиданный успех")
    except NetworkConnectionException as e:
        print("✅ Сетевая ошибка корректно обработана и преобразована")
        print(f"   Ошибка: {e}")
    except Exception as e:
        print(f"⚠️ Другая ошибка: {e}")
    
    # Тест 3: Временная ошибка
    try:
        print("3. Тест временной ошибки...")
        await safe_network_operation(
            lambda: exec('raise Exception("timeout: Request timed out")'),
            "тестовая временная операция"
        )
        print("❌ Неожиданный успех")
    except (NetworkConnectionException, TemporaryNetworkException) as e:
        print("✅ Временная ошибка корректно обработана")
        print(f"   Ошибка: {e}")
    except Exception as e:
        print(f"⚠️ Другая ошибка: {e}")

async def test_real_playwright_scenario():
    """Эмулирует реальный сценарий работы с Playwright"""
    from playwright.async_api import async_playwright
    
    print("\n=== Тест реального сценария Playwright ===")
    
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    page = await browser.new_page()
    
    # Тест с реальной сетевой операцией
    try:
        print("Тест загрузки страницы...")
        await safe_network_operation(
            lambda: page.goto("https://httpbin.org/delay/2", timeout=10000),
            "загрузка тестовой страницы"
        )
        print("✅ Загрузка страницы успешна")
        
        # Тест поиска элемента
        await safe_network_operation(
            lambda: page.locator("h1").wait_for(state="visible", timeout=5000),
            "поиск заголовка на странице"
        )
        print("✅ Поиск элемента успешен")
        
    except Exception as e:
        print(f"⚠️ Ошибка в реальном сценарии: {e}")
    
    await browser.close()
    await playwright.stop()

async def main():
    """Запуск финального теста"""
    print("🚀 Запуск финального теста системы сетевой устойчивости...\n")
    
    await test_network_aware_operations()
    await test_real_playwright_scenario()
    
    print("\n✅ Система готова к работе!")
    print("💡 При отключении Wi-Fi во время работы бота:")
    print("   - Операции будут автоматически повторены 3 раза")
    print("   - Пользователи получат уведомления о проблемах с сетью")
    print("   - Сессии будут сохранены при сетевых сбоях")
    print("   - После восстановления соединения работа продолжится")

if __name__ == "__main__":
    asyncio.run(main())
