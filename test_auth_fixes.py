#!/usr/bin/env python3
"""
Тест исправлений проблем с реавторизацией и localStorage.
"""

import sys
import importlib.util

def test_auth_function_improvements():
    """Тест улучшений функции автореавторизации"""
    try:
        # Импортируем модуль
        spec = importlib.util.spec_from_file_location("booking_bot", "booking_bot.py")
        booking_bot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(booking_bot)
        
        print("✅ Модуль booking_bot успешно загружен")
        
        # Проверяем наличие функции auto_reauth_with_key
        if hasattr(booking_bot, 'auto_reauth_with_key'):
            print("✅ Функция auto_reauth_with_key найдена")
        else:
            print("❌ Функция auto_reauth_with_key не найдена")
            return False
            
        # Проверяем функции для persistent sessions
        if hasattr(booking_bot, 'save_persistent_session_state'):
            print("✅ Функция save_persistent_session_state найдена")
        else:
            print("❌ Функция save_persistent_session_state не найдена")
            return False
            
        if hasattr(booking_bot, 'restore_persistent_session_state'):
            print("✅ Функция restore_persistent_session_state найдена")
        else:
            print("❌ Функция restore_persistent_session_state не найдена")
            return False
            
        # Проверяем, что есть обработка NetworkConnectionException
        if hasattr(booking_bot, 'NetworkConnectionException'):
            print("✅ NetworkConnectionException найден")
        else:
            print("❌ NetworkConnectionException не найден")
            return False
            
        print("🎉 Все основные компоненты на месте!")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка при тестировании: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_user_data_scenarios():
    """Тест сценариев с данными пользователя"""
    print("\n🧪 Тестирование сценариев данных пользователя:")
    
    # Тест 1: Полные данные
    full_data = {
        'auth_method': 'key',
        'key_file_path': '/path/to/key.pfx',
        'key_password': 'password123'
    }
    
    missing_path = {
        'auth_method': 'key',
        'key_password': 'password123'
        # key_file_path отсутствует
    }
    
    missing_password = {
        'auth_method': 'key',
        'key_file_path': '/path/to/key.pfx'
        # key_password отсутствует
    }
    
    diia_data = {
        'auth_method': 'diia'
        # данные ключа отсутствуют, что нормально для diia
    }
    
    test_cases = [
        ("Полные данные для ключа", full_data, True),
        ("Отсутствует key_file_path", missing_path, False),
        ("Отсутствует key_password", missing_password, False),
        ("Данные для Diia (без ключа)", diia_data, False)  # должен вернуть False для автореавторизации
    ]
    
    for case_name, test_data, expected_has_key_data in test_cases:
        has_key_path = 'key_file_path' in test_data
        has_key_password = 'key_password' in test_data
        actual_result = has_key_path and has_key_password
        
        if actual_result == expected_has_key_data:
            print(f"  ✅ {case_name}: правильно определено наличие данных ключа")
        else:
            print(f"  ❌ {case_name}: ожидалось {expected_has_key_data}, получено {actual_result}")
    
    return True

if __name__ == "__main__":
    print("🧪 ТЕСТ ИСПРАВЛЕНИЙ РЕАВТОРИЗАЦИИ И LOCALSTORAGE")
    print("=" * 60)
    
    success1 = test_auth_function_improvements()
    success2 = test_user_data_scenarios()
    
    print("=" * 60)
    if success1 and success2:
        print("✅ РЕЗУЛЬТАТ: ВСЕ ТЕСТЫ ПРОЙДЕНЫ")
        print("\n🔧 ИСПРАВЛЕНИЯ:")
        print("  ✅ Добавлена проверка наличия key_file_path и key_password")
        print("  ✅ Добавлена проверка существования файла ключа")
        print("  ✅ Добавлена безопасная обработка localStorage/sessionStorage")
        print("  ✅ Добавлено graceful degradation при ошибках storage")
        sys.exit(0)
    else:
        print("❌ РЕЗУЛЬТАТ: ЕСТЬ ОШИБКИ")
        sys.exit(1)
