#!/usr/bin/env python3
"""
Тест для проверки обнаружения реальных сетевых ошибок Playwright
"""

import asyncio
import logging
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from booking_bot import is_network_error, retry_network_operation, NetworkConnectionException

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

async def test_real_network_operations():
    """Тестирует реальные сетевые операции Playwright"""
    print("=== Тест реальных сетевых операций ===")
    
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    page = await browser.new_page()
    
    # Тест 1: Нормальная загрузка
    try:
        print("1. Тест нормальной загрузки...")
        await retry_network_operation(
            lambda: page.goto("https://httpbin.org/delay/1", timeout=10000),
            context_info="тест нормальной загрузки"
        )
        print("✅ Нормальная загрузка прошла успешно")
    except Exception as e:
        print(f"❌ Ошибка при нормальной загрузке: {e}")
    
    # Тест 2: Тайм-аут операции (симуляция медленного ответа)
    try:
        print("2. Тест тайм-аута операции...")
        await retry_network_operation(
            lambda: page.goto("https://httpbin.org/delay/15", timeout=5000),  # 15 секунд задержка, но таймаут 5 секунд
            context_info="тест тайм-аута"
        )
        print("✅ Тайм-аут обработан корректно")
    except Exception as e:
        print(f"⚠️ Ожидаемая ошибка тайм-аута: {type(e).__name__}: {e}")
        print(f"Это сетевая ошибка? {is_network_error(e)}")
    
    # Тест 3: Несуществующий домен
    try:
        print("3. Тест несуществующего домена...")
        await retry_network_operation(
            lambda: page.goto("https://nonexistent-domain-12345.com", timeout=10000),
            context_info="тест несуществующего домена"
        )
        print("✅ Несуществующий домен обработан")
    except Exception as e:
        print(f"⚠️ Ожидаемая ошибка DNS: {type(e).__name__}: {e}")
        print(f"Это сетевая ошибка? {is_network_error(e)}")
    
    # Тест 4: Клик по элементу на загруженной странице (для проверки других операций)
    try:
        print("4. Тест операций на странице...")
        await page.goto("https://httpbin.org/html", timeout=10000)
        
        # Попробуем кликнуть по элементу с тайм-аутом
        await retry_network_operation(
            lambda: page.locator("h1").wait_for(state="visible", timeout=3000),
            context_info="ожидание элемента на странице"
        )
        print("✅ Операции на странице выполнены успешно")
    except Exception as e:
        print(f"⚠️ Ошибка при операциях на странице: {type(e).__name__}: {e}")
        print(f"Это сетевая ошибка? {is_network_error(e)}")
    
    await browser.close()
    await playwright.stop()

async def test_network_disconnection():
    """Симулирует отключение сети через блокировку DNS"""
    print("\n=== Тест симуляции отключения сети ===")
    
    playwright = await async_playwright().start()
    
    # Запускаем браузер с блокировкой определенных доменов
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--host-resolver-rules=MAP * 127.0.0.1",  # Перенаправляем все запросы на localhost
            "--disable-web-security"
        ]
    )
    page = await browser.new_page()
    
    try:
        print("Попытка загрузки с заблокированной сетью...")
        await retry_network_operation(
            lambda: page.goto("https://e-consul.gov.ua", timeout=10000),
            context_info="тест с заблокированной сетью"
        )
        print("✅ Неожиданно успешно!")
    except NetworkConnectionException as e:
        print(f"✅ Корректно обнаружена сетевая ошибка: {e}")
    except Exception as e:
        print(f"⚠️ Другая ошибка: {type(e).__name__}: {e}")
        print(f"Классифицирована как сетевая: {is_network_error(e)}")
    
    await browser.close()
    await playwright.stop()

async def main():
    """Запуск всех тестов"""
    print("Начинаю тестирование обнаружения сетевых ошибок в реальных условиях...\n")
    
    await test_real_network_operations()
    await test_network_disconnection()
    
    print("\n=== Тестирование завершено ===")
    print("💡 Совет: Попробуйте отключить Wi-Fi во время работы бота для реального теста")

if __name__ == "__main__":
    asyncio.run(main())
