# Память пользователя и команды

Проект анализирует диалоги, извлекает правила, предпочтения, определения,
случаи решения задач и процедуры. Консолидация через Codex готовит изменения
в копии. LLM-ревьюер проверяет и исправляет её до переноса.

Состояние на 24 октября 2026 года: выполнены очистка старого пути обработки
и выделение общих операций запуска. Анализ создаёт два отчёта на чат; их текст
передаётся целиком. Личный этап готовит правила, контекст,
wiki, cases и skills в одной копии. Ревьюер проверяет и исправляет всю версию. Флаг --evaluate-by-replay
включает LLM-оценку контекста и затрат. Если прогон выполним и оправдан,
выполняется одно решение задачи и сравнение результатов; иначе сохраняется
причина пропуска и решение принимается по ревью. Отчёты этапов находятся
в report/ внутри каталога personal/ или knowledge/ каждого запуска.
Промежуточное хранилище и учёт командных подтверждений пока не реализованы.

## Запуск

Для анализа и консолидации через Codex нужны Python 3.11+ и авторизованный CLI.
Для прямого запуска через OpenRouter нужны DeepAgents, LangChain,
langchain-openai и переменная окружения OPENROUTER_API_KEY. Ключ в файлах
проекта не хранится.

Полный личный цикл напрямую через OpenRouter:

```powershell
$env:OPENROUTER_API_KEY = "новый-ключ"
$workspace = "data/synthetic-team-runs/20261020T040159Z-a0d71f0d/users/101/user_workspace"
python analyze_chats_openrouter.py --user-id 101 --workspace $workspace --model openai/gpt-5.6-terra
python consolidate_reports_openrouter.py --user-id 101 --workspace $workspace --stage personal --model openai/gpt-5.6-terra --evaluate-by-replay --replay-scenario docs/examples/replay-anna/scenario.json
```

Анализ выполняет один запрос без инструментов на каждый из двух отчётов чата.
Консолидация, ревью, оценка возможности прогона, прогон и сравнение используют
OpenRouter без запуска Codex CLI. Без полного сценария LLM может пропустить
дорогой или невоспроизводимый прогон и записывает причину.

Рабочая область передаётся явно. Пример для существующей Анны:

```powershell
$annaWorkspace = "data/synthetic-team-runs/20261020T040159Z-a0d71f0d/users/101/user_workspace"
python analyze_chats_codex.py --user-id 101 --workspace $annaWorkspace --validate-only
python analyze_chats_codex.py --user-id 101 --workspace $annaWorkspace
python consolidate_reports_codex.py --user-id 101 --workspace $annaWorkspace --stage personal
python consolidate_reports_codex.py --user-id 101 --workspace $annaWorkspace --stage knowledge
```

Для Бориса используйте user-id 102 и его user_workspace. Последовательные
knowledge-запуски дополняют один каталог knowledge/. --validate-only не вызывает
модели и не создаёт файлы. Без --stage выполняются оба этапа.

```powershell
python user_workspace/main.py
python run_synthetic_dialogues.py --check
python run_synthetic_dialogues.py
```

main.py использует обычную user_workspace/, а не память Анны из примера.
Генератор синтетики создаёт новый каталог запуска; передавайте полученный
user_workspace анализатору и консолидатору.

## Модули

| Файл | Ответственность |
| --- | --- |
| codex_client.py | Поиск CLI, запуск, события, ошибки, время и usage |
| openrouter_client.py | Прямой одиночный вызов и агентный запуск через OpenRouter |
| agent_runtime.py | Сборка решающего агента, подключение личных знаний, запись сообщений |
| replay_evaluation.py | Сценарий, копия окружения, один запуск и решение оценщика |
| replay_worker.py | Решение одной задачи, ответ, трасса, время, шаги и usage |
| replay_codex.py | Один прогон через Codex с выбранной моделью, ответ и измерения CLI |
| memory_pipeline.py | Чтение отчётов целиком и выгрузка истории из SQLite |
| reviewed_consolidation.py | Копии памяти, исправляющее ревью, итоговый снимок и перенос |
| analyze_chats_codex.py | Два независимых анализа каждого диалога пользователя |
| consolidate_reports_codex.py | Запуск личной и командной консолидации |
| analyze_chats_openrouter.py | Точка запуска анализа без Codex CLI |
| consolidate_reports_openrouter.py | Полный цикл консолидации и проверки без Codex CLI |
| user_workspace/main.py | Консольный диалог через agent_runtime |
| run_synthetic_dialogues.py | Восемь тестовых диалогов через тот же agent_runtime |

## Память и данные

- chat_logs.db в рабочей области содержит сообщения пользователя, агента и инструментов.
- raw/chat-reports-codex/session-ID/context.md и procedures.md — два отчёта на диалог; сведения запуска в manifest.json.
- AGENTS.md, USER_PREFERENCES.md, user_llm_wiki/ и knowledge/ внутри рабочей области — личная консолидация.
- knowledge/ в корне проекта — общие wiki, cases, skills и INDEX.md.
- data/consolidation-runs/ — снимки, задания, журналы и решения ревьюеров.
- data/synthetic-team-runs/ — чаты тестовой команды и результаты их обработки.

В исходном тестовом наборе восемь чатов четырёх участников и четыре прежних Codex-отчёта Анны и Бориса.
Общий каталог содержит 4 wiki, 4 cases, 3 skills и индекс. Известные замечания
к содержанию не исправлялись при переносе кода. Основной агент пока использует
личные AGENTS.md, USER_PREFERENCES.md, существующие индексы user_llm_wiki/
и knowledge/, а также прежние /skills/ и личные /knowledge/skills/.
Общий каталог команды пока не подключён к решающему агенту.

## Документация

- [Спецификация следующей версии: два анализа, ревью с исправлениями и будущие адаптеры](openspec/specs/single-user-memory/spec.md).
- [Как работает текущий цикл](docs/memory-pipeline.md).
- [Хранение сообщений](docs/chat-database.md).
- [Проверка до обновления памяти](docs/reviewed-consolidation.md).
- [Один проверочный прогон и выбор памяти](docs/replay-evaluation.md).
- [Ревьюер и результаты его испытаний](docs/memory-review.md).
- [Целевая схема личной и командной памяти](docs/proposed-personal-team-memory.md).
- [Порядок реализации](docs/implementation-plan-personal-team-memory.md).
- [Запуск из PyCharm](OPEN_IN_PYCHARM.md).
- [Тестовая команда](docs/examples/synthetic-team/README.md).

## Проверки без моделей

```powershell
python -X utf8 -m unittest discover -s tests
python run_synthetic_dialogues.py --check
```

Содержание оценивают LLM. Тесты Python проверяют управление процессами,
сохранение сообщений и условия переноса файлов.

## Проверка предложенной памяти решением задачи

```powershell
python consolidate_reports_codex.py --user-id 101 --workspace $annaWorkspace --stage personal --evaluate-by-replay --replay-scenario docs/examples/replay-anna/scenario.json --agent-python C:/Users/Slav4ik/PycharmProjects/deepagent/.venv/Scripts/python.exe
```

Перед запуском задайте annaWorkspace из примера выше. Сценарий Анны готов;
прежний ответ и файлы предназначены только оценщику. Без флага дополнительного
решения задачи нет. current, inconclusive и сбой сохраняют прежнюю память.
Для выполнения самой задачи через Codex добавьте `--replay-backend codex --model gpt-5.6-terra`.
Без выбора backend решающий агент по-прежнему использует DeepAgents и settings.py.
