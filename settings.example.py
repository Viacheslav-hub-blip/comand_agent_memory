"""Шаблон настроек консольного агента и тестовых диалогов.

Ключ передаётся через переменную окружения OPENROUTER_API_KEY.
Вход: окружение процесса.
Выход: настройки модели, пользователя и параллелизма.
Функций и классов нет.
"""
import os

openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "")
model = "xiaomi/mimo-v2.6-pro"
user_id = "1"
max_concurrency = 2
