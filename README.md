# Память пользователя и команды

Основной цикл следует [утверждённой схеме](docs/conference-memory-flow.html):
новые сессии → два анализа → личная консолидация → review → при включённой
проверке диалог с новой памятью → сравнение → принятие → командное объединение.

Подключение рабочего агента через `--config` читает истории Claude Code, Codex,
GigaAgent, Arize Phoenix, JSONL и SQLite и обновляет указанные пользователем
файлы памяти, в том числе вне репозитория. Источник истории и состав памяти
настраиваются независимо; новый источник или провайдер регистрируется отдельным модулем.
Для каждого модельного этапа выбираются провайдер и модель; общий текстовый файл
пожеланий добавляется к каждому запросу. Встроены исполнители OpenRouter и Codex CLI.

```powershell
python -X utf8 run_memory_pipeline.py --config docs/examples/agent-connection/connection.json --validate-only
```

Пример проверяется без ключей и моделей. Перед реальным запуском замените
`YOUR_*_MODEL_ID` и пути на свои. [Настройки, поддерживаемые форматы и расширение](docs/agent-connections.md).

Прежние команды SQLite/Codex без `--config` сохранены. Реестр подтверждений,
pending и conflicts отложены. Командный этап объединяет уже принятые личные
знания без отдельного агента-review и подключает общий снимок пользователям.

## Основная команда

Проверка входов без моделей и записи файлов:

```powershell
python -X utf8 run_memory_pipeline.py --user-id 101 --workspace data/users/101 --source sqlite --validate-only
```

Рабочая область для SQLite должна содержать chat_logs.db. Для Codex используется
существующий источник с явно выбранными проектами:

```powershell
python -X utf8 run_memory_pipeline.py --user-id slav4ik --workspace data/openrouter-runs/slav4ik --source codex --project jev_test --project offzone --validate-only
```

Уберите --validate-only только для реального запуска моделей. По умолчанию
используется OpenRouter; нужны зависимости DeepAgents/LangChain и авторизация
провайдера. Ключи в примерах не задаются. --backend codex выбирает существующий
Codex-исполнитель анализа, консолидации и оценки.

--stage personal выполняет только личный цикл; по умолчанию --stage both
включает и командное объединение. --knowledge-dir задаёт общий каталог.

## Проверка новой памяти

Флаг --evaluate-by-replay включает локальную проверку наличия данных. Если
сценария, отдельных пользовательских реплик, исходных файлов или прежнего ответа
нет, память принимается по review со статусом skipped. Модель не оценивает цену
прогона. При достаточных данных выполняется диалог; тестировщик подтверждает
конечную цель, после чего оценщик сравнивает результат с прежним.

```powershell
python -X utf8 run_memory_pipeline.py --user-id 101 --workspace data/users/101 --source sqlite --evaluate-by-replay --replay-scenario docs/examples/replay-anna/dialogue-scenario.json --validate-only
```

Для Codex полный диалоговый сценарий может быть собран автоматически из сохранённой
истории и доступных файлов. Повреждённые материалы не маскируются под отсутствие
данных. Решающий агент получает первую реплику и следующие выбранные реплики
дословно; эталон доступен тестировщику и итоговому оценщику.

В основном CLI больше нет --force-replay, --replay-backend и --agent-python.
Используйте Python того окружения, где установлены зависимости диалогового агента.

## Несколько пользователей

JSON-задания, пути относительно корня проекта:

```json
[
  {"user_id": "101", "workspace": "data/users/101", "source": "sqlite"},
  {"user_id": "102", "workspace": "data/users/102", "source": "sqlite"}
]
```

```powershell
python -X utf8 run_memory_pipeline.py --users users.json --validate-only
```

Сначала проверяются задания всех участников. Затем выполняются независимые
личные циклы и одно объединение принятых знаний. Области пользователей не
должны пересекаться. Принятие без прогона хранится отдельно от успешного прогона.

Завершённые чаты повторно не анализируются. При обычном запуске с неизменными
отчётами и памятью используются уже принятые личные и командные результаты.
Явно запрошенная проверка --evaluate-by-replay запускает новый личный цикл,
чтобы не подменять его прежним принятием без проверки.

## Отдельные этапы

Существующие команды сохранены как тонкие точки входа:

```powershell
python analyze_chats_openrouter.py --user-id 101 --workspace data/users/101 --validate-only
python consolidate_reports_openrouter.py --user-id 101 --workspace data/users/101 --stage personal --validate-only
python consolidate_reports_openrouter.py --stage knowledge --accepted-run data/consolidation-runs/ID/personal --validate-only
```

--accepted-run можно повторить для нескольких участников. Командный вход проверяет
state=published/unchanged, владельца, основание принятия и хеши снимка after/.
Исходные аналитические отчёты командному консолидатору больше не передаются.
Личные AGENTS.md, USER_PREFERENCES.md и user_llm_wiki/ не экспортируются.

Командный консолидатор работает в копии и сам сообщает accepted либо оставшиеся
проблемы. При неразрешённом противоречии сохраняется прежняя общая версия.
Это временный режим без постоянного учёта подтверждений и без голосования.
После публикации team_knowledge/ обновляется у перечисленных владельцев;
agent_runtime подключает индекс и каталог навыков в новом сеансе.

## Структура и данные

Повторно используемый код находится в command_agent_memory/:

- analysis/ — отбор сессий, анализ и версия хранилища отчётов.
- memory/ — личная память, принятие, командное объединение и CLI этапов.
- evaluation/ — сценарий, диалог и сравнение результатов.
- pipeline.py — полный цикл одного или нескольких пользователей.
- adapters/, runs/, synthetic/ — прежние исполнители, каталоги и тестовые входы.

Корневые reviewed_consolidation.py и replay_evaluation.py сохраняют прежние
импорты через тот же модуль подсистемы. Отдельные экспериментальные команды
run_dialogue_replay.py и run_memory_ablation.py сохраняют свою методику измерений
и не являются дополнительными этапами основного конвейера.

Истории: workspace/chat_logs.db. Анализ: workspace/raw/chat-reports-*/.
Личная память: AGENTS.md, USER_PREFERENCES.md, user_llm_wiki/, knowledge/.
Общие знания: knowledge/ либо --knowledge-dir. Журналы: data/consolidation-runs/.
В status.json хранятся версии входов и снимка; это технический журнал публикации,
а не реестр независимых подтверждений.

## Проверки

```powershell
python -X utf8 -m unittest discover -s tests
```

Новые сквозные тесты используют имитации исполнителей и запрещают сетевые соединения.

- [Текущая структура](docs/project-structure.md).
- [Целевая спецификация](openspec/specs/single-user-memory/spec.md).
- [Диалоговая проверка](docs/dialogue-replay.md).
- [Исторические результаты на 26 октября](docs/project-state-2026-10-26.md).
