# Запуск из PyCharm

Откройте каталог comand_agent_memory как проект. Для Codex-скриптов нужен
Python 3.11+; для основного агента выберите окружение с DeepAgents и LangChain.
При необходимости используйте существующий SDK соседнего deepagent:
C:/Users/Slav4ik/PycharmProjects/deepagent/.venv/Scripts/python.exe.

Рабочий каталог конфигурации запуска — корень comand_agent_memory.

| Скрипт | Script parameters |
| --- | --- |
| analyze_chats_codex.py | --user-id 101 --workspace data/synthetic-team-runs/20261020T040159Z-a0d71f0d/users/101/user_workspace |
| consolidate_reports_codex.py | --user-id 101 --workspace data/synthetic-team-runs/20261020T040159Z-a0d71f0d/users/101/user_workspace --stage personal |
| consolidate_reports_codex.py | Те же параметры с --stage knowledge |
| user_workspace/main.py | Без параметров; обычная user_workspace |
| run_synthetic_dialogues.py | --check для проверки файлов; без параметров для новых диалогов |

Для Бориса замените 101 на 102 в идентификаторе и пути. --workspace обязателен.
--validate-only у Codex-скриптов проверяет входы без вызова модели и записи.
Для Codex нужна его авторизация; путь при необходимости задаётся --codex-path.

Консольный агент и генератор используют settings.py. Codex-скрипты его не читают.
Анализ и консолидация запускаются отдельно. Журналы содержат задания, события,
ошибки, ответы и metrics.json с длительностью и доступным usage.

Анализ создаёт context.md и procedures.md в raw/chat-reports-codex/session-ID/.
Ревьюер может исправить предложенную копию; after сохраняется после его правок.

Для одного прогона добавьте к personal:

```text
--evaluate-by-replay --replay-scenario docs/examples/replay-anna/scenario.json
```

Выберите Python с DeepAgents либо передайте его путь через --agent-python.
Формат сценария и пример полной команды: [проверка прогоном](docs/replay-evaluation.md).
