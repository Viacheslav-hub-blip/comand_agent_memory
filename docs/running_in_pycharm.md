# Запуск в PyCharm

Руководство описывает конфигурации запуска. Функций и классов в документе нет.

1. Откройте каталог проекта и выберите Python 3.11+ из `.venv`.
2. В терминале проекта выполните `python -m pip install -e .`.
3. Создайте конфигурацию запуска Python с типом **Module name**.
4. Module name: `command_agent_memory`.
5. Parameters: `--config docs/examples/jsonl_to_file_memory/memory_pipeline.json --validate-only --json`.
6. Working directory: корень проекта.

Проверочный пример не требует ключей и не вызывает модели.
Для рабочего запуска укажите собственную конфигурацию и уберите `--validate-only`.
Чтобы проверить выбранный режим, используйте его флаг вместе с
`--validate-only`. Для replay, обработки Git-вкладов и синхронизации
предусмотрены отдельные флаги `--evaluate-by-replay`, `--team-process`
и `--team-sync`. Итоговый JSON содержит статусы и пути к журналам.
Переменные окружения провайдера задаются в конфигурации запуска PyCharm.
Необязательные локальные настройки находятся в `local_settings/agent_settings.py`.

[Учебный replay](examples/portable_replay/README.md) ·
[Роли и настройки Git](git_team_memory.md)
