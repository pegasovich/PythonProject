#!/usr/bin/env python3
"""
Тест обработки различных типов сетевых отключений
"""

import asyncio
import logging
from booking_bot import is_network_error, is_temporary_error

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def test_wifi_module_disconnection():
    """Тест ошибок при отключении WiFi модуля"""
    print("📶 Тест отключения WiFi модуля")
    print("=" * 40)
    
    wifi_module_errors = [
        # Windows
        Exception("net::ERR_INTERNET_DISCONNECTED"),
        Exception("net::ERR_NETWORK_CHANGED"),
        Exception("The network adapter is disabled"),
        Exception("WiFi is turned off"),
        
        # Playwright ошибки
        Exception("page.goto: net::ERR_INTERNET_DISCONNECTED"),
        Exception("Timeout 30000ms exceeded during navigation"),
        Exception("net::ERR_NAME_NOT_RESOLVED at page.goto"),
        
        # Python/httpx ошибки
        Exception("httpx.ConnectError: No internet connection"),
        Exception("requests.exceptions.ConnectionError: Network is unreachable"),
    ]
    
    for i, error in enumerate(wifi_module_errors, 1):
        is_network = is_network_error(error)
        is_temp = is_temporary_error(error)
        status = "✅" if is_network else "❌"
        print(f"  {status} {i}. {error}")
        print(f"      Сетевая: {is_network}, Временная: {is_temp}")
    
    return all(is_network_error(e) for e in wifi_module_errors)

def test_router_disconnection():
    """Тест ошибок при отключении от роутера"""
    print("\n🌐 Тест отключения от сети роутера")
    print("=" * 40)
    
    router_disconnection_errors = [
        # DNS ошибки
        Exception("getaddrinfo failed"),
        Exception("[Errno 11001] getaddrinfo failed"),
        Exception("httpx.ConnectError: [Errno 11001] getaddrinfo failed"),
        Exception("telegram.error.NetworkError: httpx.ConnectError: [Errno 11001] getaddrinfo failed"),
        
        # Timeout ошибки (роутер не отвечает)
        Exception("Connection timeout"),
        Exception("Request timeout"),
        Exception("httpx.TimeoutError: Request timed out"),
        Exception("Read timeout"),
        
        # Connection ошибки (роутер недоступен)
        Exception("Connection refused"),
        Exception("Connection reset by peer"),
        Exception("No route to host"),
        Exception("Host unreachable"),
        Exception("Network is unreachable"),
        
        # HTTP ошибки (роутер блокирует)
        Exception("502 Bad Gateway"),
        Exception("503 Service Unavailable"),
        Exception("504 Gateway Timeout"),
    ]
    
    for i, error in enumerate(router_disconnection_errors, 1):
        is_network = is_network_error(error)
        is_temp = is_temporary_error(error)
        status = "✅" if is_network else "❌"
        print(f"  {status} {i}. {error}")
        print(f"      Сетевая: {is_network}, Временная: {is_temp}")
    
    return all(is_network_error(e) for e in router_disconnection_errors)

def test_mixed_scenarios():
    """Тест смешанных сценариев"""
    print("\n🔀 Тест смешанных сценариев")
    print("=" * 40)
    
    mixed_errors = [
        # Переключение сетей
        ("Переключение WiFi → мобильная", Exception("net::ERR_NETWORK_CHANGED")),
        ("Слабый сигнал WiFi", Exception("Request timeout")),
        ("Перегрузка роутера", Exception("Connection timeout")),
        ("DNS сервер недоступен", Exception("DNS resolution failed")),
        ("Блокировка провайдером", Exception("Connection refused")),
        ("Фаервол блокирует", Exception("Host unreachable")),
        
        # Не сетевые ошибки (для контроля)
        ("Ошибка в коде", Exception("ValueError: invalid input")),
        ("Файл не найден", Exception("FileNotFoundError: file.txt")),
        ("Проблема с DOM", Exception("Element not found")),
    ]
    
    network_count = 0
    total_network_expected = 6  # Первые 6 должны быть сетевыми
    
    for i, (description, error) in enumerate(mixed_errors, 1):
        is_network = is_network_error(error)
        expected_network = i <= total_network_expected
        status = "✅" if is_network == expected_network else "❌"
        
        print(f"  {status} {i}. {description}")
        print(f"      Ошибка: {error}")
        print(f"      Сетевая: {is_network} (ожидалось: {expected_network})")
        
        if is_network and expected_network:
            network_count += 1
    
    return network_count == total_network_expected

def main():
    """Основная функция тестирования"""
    print("🧪 ТЕСТ ОБРАБОТКИ ТИПОВ СЕТЕВЫХ ОТКЛЮЧЕНИЙ")
    print("🎯 Проверяем: WiFi модуль vs отключение от роутера")
    print("=" * 60)
    
    try:
        # Тестируем каждый тип отключения
        wifi_ok = test_wifi_module_disconnection()
        router_ok = test_router_disconnection() 
        mixed_ok = test_mixed_scenarios()
        
        print("\n" + "=" * 60)
        print("📊 РЕЗУЛЬТАТЫ ТЕСТИРОВАНИЯ:")
        print(f"   📶 Отключение WiFi модуля: {'✅ ВСЕ ОБРАБОТАНЫ' if wifi_ok else '❌ ЕСТЬ ПРОПУСКИ'}")
        print(f"   🌐 Отключение от роутера: {'✅ ВСЕ ОБРАБОТАНЫ' if router_ok else '❌ ЕСТЬ ПРОПУСКИ'}")
        print(f"   🔀 Смешанные сценарии: {'✅ ВСЕ КОРРЕКТНЫ' if mixed_ok else '❌ ЕСТЬ ОШИБКИ'}")
        
        if wifi_ok and router_ok and mixed_ok:
            print("\n🎉 ВЫВОД: Система обрабатывает ОБА типа отключений!")
            print("✅ WiFi модуль отключение - ОБРАБОТАНО")
            print("✅ Отключение от роутера - ОБРАБОТАНО") 
            print("✅ Все сетевые ошибки правильно классифицированы")
        else:
            print("\n⚠️  ВНИМАНИЕ: Обнаружены пропуски в обработке")
            if not wifi_ok:
                print("❌ Некоторые ошибки WiFi модуля не распознаются")
            if not router_ok:
                print("❌ Некоторые ошибки роутера не распознаются")
            if not mixed_ok:
                print("❌ Ошибки в классификации смешанных сценариев")
        
    except Exception as e:
        print(f"\n❌ Ошибка при тестировании: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
