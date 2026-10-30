# Запуск в PyCharm

1. Откройте каталог проекта и выберите Python 3.11+ из `.venv`.
2. В терминале проекта выполните `python -m pip install -e .`.
3. Создайте конфигурацию запуска Python с типом **Module name**.
4. Module name: `command_agent_memory`.
5. Parameters: `--config docs/examples/jsonl_to_file_memory/memory_pipeline.json --validate-only`.
6. Working directory: корень проекта.

Проверочный пример не требует ключей и не вызывает модели.
Для рабочего запуска укажите собственную конфигурацию и уберите `--validate-only`.
Переменные окружения провайдера задаются в конфигурации запуска PyCharm.
Необязательные локальные настройки находятся в `local_settings/agent_settings.py`.
