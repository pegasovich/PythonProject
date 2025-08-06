#!/usr/bin/env python3
"""
Тестовый скрипт для проверки обработки сетевых ошибок
Имитирует работу воркера с отправкой Telegram сообщений
"""

import asyncio
import logging
from telegram import Bot
from booking_bot import safe_send_message, NetworkConnectionException, TemporaryNetworkException

# Настройка логирования
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('network_test.log'),
        logging.StreamHandler()
    ]
)

# Замените на ваш реальный токен бота для тестирования
BOT_TOKEN = "8435412398:AAHyipKP5OjjTolIMvSiC9z6Blz7GAO6Bgu4"
TEST_CHAT_ID = 193287726  # Ваш chat_id

async def test_network_resilience():
    """Тест сетевой устойчивости с реальным Telegram API"""
    print("🔬 Тест сетевой устойчивости с реальным Telegram API")
    print("📱 Отключите WiFi через 10 секунд...")
    
    bot = Bot(token=BOT_TOKEN)
    
    # Даем время на подготовку
    for i in range(10, 0, -1):
        print(f"⏰ Осталось {i} секунд до начала теста...")
        await asyncio.sleep(1)
    
    print("🚀 Начинаю тест! Отключите WiFi сейчас!")
    
    # Пытаемся отправить сообщения каждые 5 секунд
    for i in range(1, 21):  # 20 попыток (100 секунд)
        try:
            print(f"\n📤 Попытка отправки сообщения {i}/20")
            await safe_send_message(
                bot, 
                TEST_CHAT_ID, 
                f"🔬 Тестовое сообщение #{i}\n"
                f"⏰ Время: {asyncio.get_event_loop().time():.0f}\n"
                f"🌐 Тест сетевой устойчивости",
                f"тест сообщения {i}"
            )
            print(f"   ✅ Сообщение {i} отправлено успешно")
            
        except NetworkConnectionException as e:
            print(f"   🌐 Сетевая ошибка в сообщении {i}: {e}")
            logging.warning(f"NetworkConnectionException в попытке {i}: {e}")
            
        except TemporaryNetworkException as e:
            print(f"   ⏳ Временная сетевая ошибка в сообщении {i}: {e}")
            logging.warning(f"TemporaryNetworkException в попытке {i}: {e}")
            
        except Exception as e:
            print(f"   ❌ Неожиданная ошибка в сообщении {i}: {e}")
            logging.error(f"Неожиданная ошибка в попытке {i}: {e}")
        
        # Ждем между попытками
        print(f"   ⏸️  Пауза 5 секунд...")
        await asyncio.sleep(5)
    
    print("\n🏁 Тест завершен!")
    print("📋 Результаты теста:")
    print("   - Проверьте логи в network_test.log")
    print("   - Проверьте, пришли ли сообщения в Telegram")
    print("   - Убедитесь, что система правильно обработала сетевые ошибки")

async def main():
    """Основная функция"""
    try:
        await test_network_resilience()
    except KeyboardInterrupt:
        print("\n🛑 Тест прерван пользователем")
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    print("🧪 ТЕСТ СЕТЕВОЙ УСТОЙЧИВОСТИ")
    print("=" * 50)
    print("📋 Инструкции:")
    print("1. Скрипт начнет отправлять Telegram сообщения")
    print("2. Отключите WiFi когда увидите сообщение")
    print("3. Через 30-60 секунд включите WiFi обратно")
    print("4. Наблюдайте за обработкой ошибок и восстановлением")
    print("=" * 50)
    
    asyncio.run(main())
