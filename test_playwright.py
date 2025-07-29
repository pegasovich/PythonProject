# Содержимое для файла test_playwright.py (БЕЗ STEALTH)

import asyncio
from playwright.async_api import async_playwright
# from playwright_stealth import stealth # <--- ОТКЛЮЧЕНО

async def main():
    print("--- Запуск тестового скрипта Playwright (без stealth) ---")
    p = None
    try:
        p = await async_playwright().start()
        print("Playwright запущен успешно.")

        browser = await p.chromium.launch(headless=False)
        print("Браузер запущен.")

        page = await browser.new_page()
        print("Новая страница создана.")

        # await stealth(page) # <--- ОТКЛЮЧЕНО
        print("Шаг применения stealth пропущен.")

        await page.goto("https://www.google.com")
        print("Перешел на google.com.")

        await page.screenshot(path="test_screenshot.png")
        print("Скриншот 'test_screenshot.png' сохранен.")

        await asyncio.sleep(5)

        await browser.close()
        print("Браузер закрыт.")

    except Exception as e:
        print(f"\n!!! Произошла ошибка: {e}")
        import traceback
        traceback.print_exc()

    finally:
        if p:
            await p.stop()
            print("Playwright остановлен.")

    print("--- Тестовый скрипт завершен ---")

if __name__ == "__main__":
    asyncio.run(main())