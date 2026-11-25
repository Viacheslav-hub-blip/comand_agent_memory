# Устройство приложения управления памятью

Приложение работает отдельно от рабочего агента. Пользователь задаёт источник
истории, документы для обновления и каталог результатов. Установка приложения
в проект рабочего агента не требуется.

Документ описывает текущую ответственность модулей и содержит таблицу прежних
импортов. Функций и классов в документе нет.

## Файлы верхнего уровня пакета

| Файл | Ответственность |
| --- | --- |
| `__main__.py` | Режимы CLI, вызов конвейера, итоговый JSON и код завершения |
| `working_agent.py` | Общая фабрика DeepAgents для обычных задач и replay, сессия с закреплённой командной версией |
| `worker_agents.py` | Контракт WorkerSession, загрузка фабрик и сопоставимость профилей |
| `application_config.py` | Чтение JSON, проверка настроек, разрешение путей, `StageSettings` и совместимость с локальными настройками |
| `memory_pipeline.py` | Проверка подключений, независимые личные циклы, повторное использование результатов и выбор командного режима |
| `skill_resources.py` | Поиск встроенных skills и чтение их инструкций; `ROOT` сохраняется для существующих проверок путей |
| `run_reports.py` | Уникальные каталоги запусков, Markdown-отчёты, diff и сводка состояний этапов |

## Получение и анализ истории

`history_sources/` получает данные без модельного анализа:

| Файл | Ответственность |
| --- | --- |
| `conversation_history.py` | `HistoryEvent`, контракт `HistorySource`, встроенные адаптеры, их регистрация и экспорт событий |
| `sql_history_source.py` | Чтение SQL через внешний DB-API драйвер и преобразование значений в JSON |

`history_analysis/` извлекает сведения из прочитанных диалогов:

| Файл | Ответственность |
| --- | --- |
| `session_analysis.py` | `analyze_source`: анализ контекста и процедур, сохранение отчётов сессий и версий входов |
| `analysis_reports.py` | `read_reports` и `report_fingerprint`: чтение завершённых отчётов и проверка их версии |

## Исполнение модельных заданий

`model_execution/` связывает модельный этап с конкретным исполнителем:

| Файл | Ответственность |
| --- | --- |
| `provider_registry.py` | `Runner`, `ModelAdapter`, регистрация и выбор провайдера |
| `stage_runner.py` | `StageRuntime`, общие пожелания, запуск этапа и журнал его настроек |
| `codex_cli.py` | Поиск и запуск Codex CLI, сохранение ответа и доступных измерений |
| `openrouter_api.py` | Текстовые вызовы OpenRouter и файловые задачи через DeepAgents |
| `local_shell.py` | Файловый backend и локальные команды, Windows-кодировки и тайм-ауты |
| `execution_policy.py` | Контекст области записи и бюджета; остановка дерева дочернего процесса |
| `scoped_filesystem.py` | Файловые инструменты OpenRouter с ограниченной записью, без shell |
| `runtime_environment.py` | Фактическая ОС, Python, рабочий каталог и правила путей в задании агента |

## Подготовка и принятие памяти

`memory_update/` определяет изменения документов и условия их принятия:

| Файл | Ответственность |
| --- | --- |
| `personal_memory_update.py` | `consolidate_with_review`: полный цикл обновления памяти одного человека |
| `team_memory_update.py` | Подготовка командного предложения для файловой и Git-публикации |
| `memory_proposal.py` | `MemoryProposal` и `prepare_memory`: подготовка изменений в рабочей копии |
| `memory_review.py` | Проверка формата ответа ревьюера, исправление копии и решение о принятии |

Память человека включает его инструкции, предпочтения и предметные знания.
Рабочая копия проходит ревью и необязательный парный replay.
Изменения публикуются после принятия. Отдельное командное LLM-ревью не добавлено.

## Документы, снимки и публикация

`memory_storage/` предоставляет операции хранения независимо от модельного цикла:

| Файл | Ответственность |
| --- | --- |
| `memory_layout.py` | `MemoryLayout`: разрешённые пути, их назначение и проверки пересечений |
| `storage_registry.py` | `MemoryStore`, регистрация и выбор хранилища |
| `file_storage.py` | Файловые снимки и публикация с резервированием; прежний `publish_memory` для вызовов без layout |
| `file_transaction.py` | Устойчивый журнал, установка файлов без затирания внешних записей и восстановление после остановки |
| `sqlite_storage.py` | Снимки документов SQLite и транзакционная публикация |
| `memory_snapshots.py` | `read_memory` и `write_snapshot` для чтения и материализации рабочих снимков |
| `accepted_knowledge.py` | Проверка статуса, основания принятия и хешей личного снимка для командного импорта |
| `memory_locking.py` | `memory_lock`: блокировка одновременного обновления области на одном устройстве |
| `team_registry.py` | Источники и версии знаний, проверка состояний и сборка действующей командной памяти |

Модули хранения не импортируют `memory_update/`. Подготовка снимка резервной
копии доступна хранилищам напрямую. Рабочие модели изменяют копию документов.

## Доставка командной памяти

`team_memory_delivery/local_copy.py` содержит существующую `sync_team_memory`:
она копирует опубликованную версию в `team_knowledge/` рабочего агента.
Основной CLI вызывает её после принятия командной версии для каталогов из
`team.workspaces`, включая повторное использование принятого результата.
Получатели могут находиться вне проекта; проверяются пересечения и ссылки.
Хеши доставленного снимка и резервная копия сохраняются в журнале доставки.
Git-доставка разделена между `contributions.py` (пакет и очередь),
`git_repository.py` (операции Git без checkout) и `git_team.py` (обработка,
кэш и синхронизация). Git-доставка принимает обработчик командного предложения
из модельного цикла. Рабочий порядок описан в [руководстве](git_team_memory.md).

## Проверка повторным решением задачи

`memory_replay/` отвечает за проверочный прогон:

| Файл | Ответственность |
| --- | --- |
| `replay_scenario.py` | `ReplayScenario`, чтение исходных реплик и проверка наличия входных данных |
| `replay_agent.py` | Запись сообщений, продолжение сессии и совместимый импорт общей сборки |
| `dialogue_runner.py` | Диалог с тестировщиком, сбор артефактов и измерений |
| `replay_evaluation.py` | Два свежих запуска одной фабрики с прежней и предложенной памятью, сбор сравнения |

Эталон остаётся доступен тестировщику и оценщику. Публикация после выполненного
replay требует `candidate`. Для явно запрошенного replay требуются все входы сценария.

## Инструкции и настройки экземпляра

`command_agent_memory/skills/` содержит встроенные инструкции этапов.
Извлечённые из опыта skills сохраняются в настроенной памяти. Проектный
[скилл update-agent-memory](../.agents/skills/update-agent-memory/SKILL.md) передаёт
модельные этапы штатным субагентам клиента через локальные `task.json`.
Скрипт `scripts/build_memory_skill.py` собирает комплект с конвейером и инструкциями.

Оба ZIP включают отдельный скилл
[agent-memory-setup](../.agents/skills/agent-memory-setup/SKILL.md).
Скрипт `setup_memory.py` предлагает подключение, проверяет чтение истории и
сохраняет проверенные настройки проекта. Для Codex и Claude Code
`project_sessions.py` находит последнюю сессию при каждом запуске по конфигу.

`memory_pipeline.example.json` — обезличенный шаблон. Скопируйте его в
`memory_pipeline.json`, замените `USER_ID`, модель и внешние пути. CLI читает
конфигурацию, явно переданную через `--config`. Скилл поддерживает запуск по
истории текущего проекта и поиск `memory_pipeline.json` в корне проекта.

`users[].workspace` задаёт каталог обработки нашим приложением, например
`./runs/USER_ID`. Рабочие документы агента задаются через `memory.root`.
Относительные пути разрешаются от каталога конфигурации. Ключи моделей
передаются через окружение. Локальные `memory_pipeline.json` и `runs/`
исключены из Git.

`tests/` проверяет поведение приложения, `docs/` описывает его настройку,
`pyproject.toml` задаёт зависимости и сборку, `README.md` содержит начало работы.
Файлы `__init__.py` обозначают группы Python-модулей и описывают их назначение.

## Перенос импортов

| Раньше | Сейчас |
| --- | --- |
| `pipeline_config.StageRuntime` | `model_execution.stage_runner.StageRuntime` |
| `pipeline_config.StageSettings` | `application_config.StageSettings` |
| `conversation_history` | `history_sources.conversation_history` |
| `sql_history_source` | `history_sources.sql_history_source` |
| `model_providers` | `model_execution.provider_registry` |
| `memory.personal_memory.MemoryProposal` | `memory_update.memory_proposal.MemoryProposal` |
| `memory.personal_memory.prepare_memory` | `memory_update.memory_proposal.prepare_memory` |
| `memory.personal_memory.review_memory` | `memory_update.memory_review.review_memory` |
| `memory.personal_memory.consolidate_with_review` | `memory_update.personal_memory_update.consolidate_with_review` |
| `memory.personal_memory.read_memory/write_snapshot` | `memory_storage.memory_snapshots.read_memory/write_snapshot` |
| `memory.personal_memory.memory_lock` | `memory_storage.memory_locking.memory_lock` |
| `memory.personal_memory.publish_memory` | `memory_storage.file_storage.publish_memory` |
| `memory.team_memory.sync_team_memory` | `team_memory_delivery.local_copy.sync_team_memory` |
| `memory.file_storage.MemoryLayout` | `memory_storage.memory_layout.MemoryLayout` |
| `memory_evaluation` | `memory_replay` |
| `project_paths.get_skill_path` | `skill_resources.get_skill_path` |

Все пути в таблице находятся внутри `command_agent_memory`.
