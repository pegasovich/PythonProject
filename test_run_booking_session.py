#!/usr/bin/env python3
"""
Простой тест для проверки, что функция run_booking_session загружается корректно.
"""

import sys
import importlib.util

def test_run_booking_session_import():
    """Тест импорта функции run_booking_session"""
    try:
        # Импортируем модуль
        spec = importlib.util.spec_from_file_location("booking_bot", "booking_bot.py")
        booking_bot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(booking_bot)
        
        # Проверяем наличие функции
        if hasattr(booking_bot, 'run_booking_session'):
            print("✅ Функция run_booking_session найдена")
            
            # Проверяем, что это callable
            if callable(booking_bot.run_booking_session):
                print("✅ run_booking_session является вызываемой функцией")
                
                # Проверяем сигнатуру функции
                import inspect
                sig = inspect.signature(booking_bot.run_booking_session)
                params = list(sig.parameters.keys())
                expected_params = ['bot', 'user_id', 'user_data', 'worker_id', 'headless_mode']
                
                if params == expected_params:
                    print("✅ Сигнатура функции корректна")
                    print(f"   Параметры: {params}")
                else:
                    print(f"❌ Неправильная сигнатура функции")
                    print(f"   Ожидалось: {expected_params}")
                    print(f"   Получено: {params}")
                    return False
                
                # Проверяем, что это async функция
                if inspect.iscoroutinefunction(booking_bot.run_booking_session):
                    print("✅ run_booking_session является async функцией")
                else:
                    print("❌ run_booking_session не является async функцией")
                    return False
                    
            else:
                print("❌ run_booking_session не является вызываемой")
                return False
        else:
            print("❌ Функция run_booking_session не найдена")
            return False
            
        # Проверяем наличие обработки сетевых ошибок
        if hasattr(booking_bot, 'is_network_error'):
            print("✅ Функция is_network_error найдена")
        else:
            print("❌ Функция is_network_error не найдена")
            return False
            
        if hasattr(booking_bot, 'NetworkConnectionException'):
            print("✅ NetworkConnectionException найден")
        else:
            print("❌ NetworkConnectionException не найден")
            return False
            
        print("🎉 Все проверки пройдены успешно!")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка при тестировании: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    print("🧪 ТЕСТ ЗАГРУЗКИ ФУНКЦИИ run_booking_session")
    print("=" * 60)
    
    success = test_run_booking_session_import()
    
    print("=" * 60)
    if success:
        print("✅ РЕЗУЛЬТАТ: ВСЕ ТЕСТЫ ПРОЙДЕНЫ")
        sys.exit(0)
    else:
        print("❌ РЕЗУЛЬТАТ: ЕСТЬ ОШИБКИ")
        sys.exit(1)
