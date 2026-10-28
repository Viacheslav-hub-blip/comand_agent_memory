# Подключение рабочего агента

Проект читает историю рабочего агента, анализирует её, готовит изменения памяти
в отдельной копии и переносит принятую после ревью версию в подключённые файлы.
Источник истории, файлы памяти и провайдер анализа настраиваются независимо.

## Быстрый старт

Проверка включённого примера без моделей, ключей API и создания файлов:

```powershell
python -X utf8 run_memory_pipeline.py --config docs/examples/agent-connection/connection.json --validate-only
```

Для рабочего подключения скопируйте конфигурацию в удобное место. Укажите
реальные `provider` и `model`, источник логов и свои пути памяти. Все относительные
пути разрешаются от каталога JSON. Поддерживаются абсолютные пути и `~`.
Пути внутри `personal` и `knowledge` задаются относительно `memory.root`, с `/`
в качестве разделителя, без `..`. Корень памяти может находиться вне репозитория.

```json
{
  "defaults": {"provider": "openrouter", "model": "YOUR_MODEL_ID"},
  "stages": {
    "context": {"provider": "openrouter", "model": "YOUR_CONTEXT_MODEL_ID"},
    "review": {"provider": "codex", "model": "YOUR_CODEX_MODEL_ID"}
  },
  "wishes_file": "wishes.txt",
  "users": [{
    "user_id": "alice",
    "workspace": "./memory-runs/alice",
    "source": {
      "type": "claude",
      "path": "C:/Users/alice/.claude/projects",
      "projects": ["my-project"]
    },
    "memory": {
      "root": "D:/work/my-project",
      "personal": ["CLAUDE.md"],
      "knowledge": [".claude/project-knowledge"]
    }
  }]
}
```

`YOUR_*_MODEL_ID` — заменяемые обозначения, а не названия существующих моделей.
Каталоги `knowledge` следует выбирать из тех, которые рабочий агент уже читает,
либо явно подключить их в его инструкции. Проект обновляет заданные файлы, но
не меняет настройки стороннего агента и не предполагает автоматическую загрузку
произвольного каталога этим агентом.

После проверки выполните команду без `--validate-only` для настоящего анализа:

```powershell
python -X utf8 run_memory_pipeline.py --config D:/settings/connection.json
```

Режим `--config` использует настройки из JSON. С ним допускаются только
`--validate-only` и `--evaluate-by-replay`. Прежние команды без `--config` сохранены.

## Настройки этапов

| Имя этапа | Назначение |
| --- | --- |
| `context` | Анализ контекста, правил и предпочтений |
| `procedures` | Анализ процедур, cases и skills |
| `consolidation` | Обновление путей `memory.personal` |
| `knowledge` | Обновление путей `memory.knowledge` |
| `review` | Проверка и исправление всей предложенной памяти |
| `replay_agent` | Решающий агент проверочного диалога |
| `replay_tester` | Агент, выбирающий следующую исходную реплику и проверяющий цель |
| `evaluation` | Сравнение нового результата с прежним |
| `team` | Объединение принятых знаний пользователей |

Для каждого этапа задаются `provider` и `model`. Общие значения можно объявить
в `defaults`; явно заданные поля этапа заменяют соответствующие общие поля.
Без defaults нужно перечислить все используемые этапы. Пустые personal либо
knowledge отключают соответствующий консолидатор; хотя бы одна область обязательна.

Встроены исполнители `openrouter` и `codex`. Для OpenRouter нужны зависимости
`deepagents`, `langchain-openai` и авторизация через `OPENROUTER_API_KEY` либо
существующий `settings.py`. Для Codex нужен авторизованный CLI; необязательный
`executable` задаёт путь к нему. Прямые API других провайдеров не встроены;
модели разных поставщиков можно выбирать через OpenRouter либо добавить адаптер.

Файл `wishes_file` необязателен. Это обычный UTF-8 текст, добавляемый в каждый
запрос каждого модельного этапа, включая повторные ходы диалога. Файл читается
один раз перед обработкой пользователей; его изменение применяется в следующем
запуске. Указанный, но недоступный файл вызывает ошибку. Точный текст сохраняется
в `configuration/wishes.txt`, настройки — в `configuration/stages.json`.
Вызовы дополнительно содержат `.stage.json` с провайдером, моделью и хешем настроек.
Секреты в конфигурации не задаются; токены API источников читаются из окружения.

## Источники истории

| `source.type` | Поддерживаемый вход |
| --- | --- |
| `claude` | Файл или каталог транскриптов Claude Code `.jsonl`, включая отдельные файлы субагентов |
| `codex` | Файл или каталог rollout `.jsonl` / `.jsonl.zst`, включая архивные файлы |
| `gigaagent` | LangGraph API истории явно выбранных threads или описанный ниже JSON-экспорт |
| `phoenix` | REST API spans проекта либо JSONL с одним Phoenix span в каждой строке |
| `jsonl` | Произвольные JSON-объекты с настраиваемым полем сессии |
| `sqlite` | Read-only SQLite и настраиваемый SELECT |

Общие фильтры: `session_ids` — список ID, `projects` — имена проектов Claude/Codex,
`limit` — максимальное число выбранных сессий в порядке, возвращённом источником.
Лимит не означает выбор последних по времени. Неизвестные ID, проекты, параметры,
повреждённые JSON и неполные цепочки checkpoints вызывают ошибку.

Claude Code: путь указывается до `projects`, каталога конкретного проекта либо
одного файла сессии. Проект определяется по `cwd` записи. `include_subagents`
по умолчанию true; события субагентов имеют отдельную пометку и сохраняют
родительские UUID. Внутренний формат транскриптов версионно зависим.

Codex: индекс `state_5.sqlite` не требуется; читаются сами rollout. Дублирующие
`event_msg` не добавляются как реплики, но `token_count` сохраняется. Несжатая
копия файла предпочтительнее одноимённой Zstandard-копии; одинаковые ID сессий
при обходе каталога объединяются. Для `.zst` нужен `zstandard`.
Поток stdout команды `codex exec --json` имеет другую схему и этим адаптером
не принимается.

GigaAgent API:

```json
{"type":"gigaagent", "url":"http://localhost:9090/api", "token_env":"GIGA_AGENT_TOKEN", "thread_ids":["THREAD_ID"]}
```

`url` — базовый адрес LangGraph API вашего развёртывания. Используется
`POST threads/{thread_id}/history` с пагинацией `before`. `token_env` можно
опустить, если сервер не требует Bearer-токен. Сохраняется цепочка родителей
выбранного head checkpoint; `checkpoint_id` позволяет выбрать конкретную ветку.
Другие ветки не присоединяются. Повтор сообщения с тем же ID исключается,
изменённая версия помечается `message_update`. Если ID сообщений отсутствуют,
выдаётся полный checkpoint с пометкой контекста, без догадок о тождестве реплик.

Для автономной обработки можно сохранить ответы API в файл:

```json
{"threads":[{"thread_id":"THREAD_ID","history":[
  {"checkpoint":{"checkpoint_id":"c1"},"parent_checkpoint":null,"values":{"messages":[{"id":"m1","type":"human","content":"Запрос"}]}}
]}]}
```

Затем использовать `{"type":"gigaagent","path":"giga-history.json"}`.
Это формат нашего контейнера экспорта вокруг исходных `ThreadState` API.

Phoenix API:

```json
{"type":"phoenix", "url":"http://localhost:6006", "project":"default", "token_env":"PHOENIX_API_KEY"}
```

Используется `GET /v1/projects/{project}/spans`, страницы `data/next_cursor`.
Для экспорта задайте `{"type":"phoenix","path":"spans.jsonl"}`. Ожидаются
`context.trace_id`, `context.span_id`, `parent_id`, `attributes` и остальные поля
Phoenix span, не бинарный OTLP и не CSV из интерфейса. Плоские и вложенные
атрибуты поддерживаются. Сессии определяются по `session.id`; он распространяется
на spans той же trace. Без session.id trace обозначается как `trace:ID`, а не
считается восстановленным полным диалогом. Входные сообщения span остаются
контекстом LLM-вызова, а не новыми пользовательскими репликами.

Произвольные JSONL и SQLite:

```json
{"type":"jsonl","path":"events.jsonl","session_field":"conversation.id","user_field":"owner","user_id":"alice"}
```

```json
{"type":"sqlite","path":"logs.db","query":"SELECT * FROM my_logs WHERE owner = :owner ORDER BY sequence","parameters":{"owner":"alice"},"session_field":"conversation_id"}
```

Каждая строка SELECT должна содержать поле сессии; остальные поля сохраняются
в `HistoryEvent.data`. По умолчанию session_field=`session_id`, user_field=`user_id`,
SQLite-запрос — `SELECT * FROM messages ORDER BY id`. База открывается read-only.
Строки data URI base64 заменяются маркерами; блоки reasoning/thinking Claude/Codex
исключаются. Текст сообщений и инструментов не сокращается.

## Применение памяти и повторный запуск

Журналы хранятся в `workspace`, память — в `memory.root`. Они могут находиться
вне репозитория; рабочий каталог может быть внутри проекта агента, если он не
пересекается с выбранными путями памяти. Области разных пользователей не пересекаются.
Символические ссылки и Windows reparse points в пути памяти не допускаются.

Консолидаторы и ревьюер работают с копией. Неоговорённые файлы из оригинального
проекта в неё не копируются и не публикуются. Перед записью сравнивается текущая
версия памяти со снимком before; проверка каждого изменяемого файла повторяется
перед его заменой. При сбое записи выполняется откат уже заменённых файлов.
Резервная копия сохраняется в `personal/external-backup`. Публикация нескольких
файлов не является атомарной для одновременно читающего стороннего процесса.

При повторном запуске сравниваются история, инструкции анализа, настройки и
пожелания. Готовые отчёты неизменных сессий используются повторно. Принятая память
используется повторно только при совпадении отчётов, настроек, состава памяти,
исходного target и хешей текущей памяти и сохранённого after.

## Проверочный диалог и командное объединение

Для диалога укажите `replay_scenario` у пользователя и добавьте
`--evaluate-by-replay`. Формат сценария описан в [dialogue-replay.md](dialogue-replay.md).
В режиме подключения сценарий и связанные пути должны находиться в каталоге
конфигурации либо его подкаталогах. Все три модельных роли имеют независимые
настройки. При каждом ходе решающему исполнителю передаётся накопленный диалог
и рабочая копия файлов; эталон доступен только тестировщику и итоговому оценщику.
Автоматической сборки replay-сценария из сторонних источников в этом режиме нет.

Проверка наличия входных данных остаётся локальной, без модели. При нехватке
сценария прогон пропускается с явным статусом; память принимается по ревью.
Для настроенных исполнителей исходные измерения доступны в `.metrics.json`
каждого вызова. Неполные суммарные model_calls/usage диалога записываются как null,
а не заменяются придуманными счётчиками.

Необязательный `"team":{"root":"./shared-memory"}` включает отдельный этап
`team`. Он получает только принятые файлы `memory.knowledge` пользователей.
Личная память не передаётся. Общий каталог подключается к рабочим агентам
средствами самих агентов; автоматической правки их конфигурации нет.

## Расширение

Источник реализует `list_sessions()` и `iter_events(session)`. Событие содержит
`source`, `session_id`, `event_id`, `kind`, `data`, время, родителя и метаданные.
Остальные части конвейера не зависят от схемы исходной БД.

Создайте импортируемый модуль, например `my_history.py`:

```python
"""Пример своего адаптера. Класс MyHistory читает сессии и события внешней системы."""
from command_agent_memory.history import HistoryEvent, register_history_source


class MyHistory:
    """Принимает параметры и каталог; предоставляет историю внешней системы."""

    def __init__(self, options, base):
        """Принимает настройки; сохраняет параметры подключения, возвращает None."""
        self.options = options
        self.base = base

    def list_sessions(self):
        """Без входов возвращает метаданные доступных сессий."""
        return [{"id": "example"}]

    def iter_events(self, session):
        """Принимает сессию; выдаёт события общего формата."""
        yield HistoryEvent("my_history", session["id"], "1", "message",
                           {"role": "user", "content": "Данные из вашей системы"})


register_history_source("my_history", MyHistory)
```

Добавьте `"extensions":["my_history"]` в конфигурацию и
`"source":{"type":"my_history"}` в подключение. Модуль должен находиться в
Python import path и содержать реализацию чтения вашей системы вместо примера.
Импорт выполняется явно по доверенной конфигурации, включая validate-only.

Для нового провайдера аналогично вызовите
`register_model_adapter("my_provider", factory)` из `command_agent_memory.adapters`.
Фабрика принимает необязательный путь CLI и возвращает `ModelAdapter` с
`agent_runner` для файловых задач и `chat_runner` для текстовых. Оба возвращают
строку ответа. Новая структура файлов памяти задаётся конфигурацией
`personal/knowledge` и не требует отдельного адаптера.

## Источники спецификаций

Проверены 28 октября 2026 года:

- [Claude Code: хранение сессий и изменчивость внутреннего формата](https://code.claude.com/docs/en/sessions).
- [Codex: формат exec --json, отличный от rollout](https://learn.chatgpt.com/docs/non-interactive-mode).
- [GigaAgent: архитектура и LangGraph](https://github.com/ai-forever/giga_agent/blob/main/docs-site/docs/developer/architecture.md).
- [LangGraph: API get_history](https://reference.langchain.com/python/langgraph-sdk/_sync/threads/SyncThreadsClient/get_history).
- [Phoenix: экспорт spans](https://arize.com/docs/phoenix/tracing/how-to-tracing/importing-and-exporting-traces/extract-data-from-spans).
- [Phoenix: реализация REST API spans](https://github.com/Arize-ai/phoenix/blob/main/src/phoenix/server/api/routers/v1/spans.py).
- [OpenInference: атрибуты сообщений, инструментов и сессий](https://arize-ai.github.io/openinference/spec/semantic_conventions.html).

Форматы проверяются автоматическими тестами на локальных примерах и имитациях
API. Совместимость с конкретным сервером GigaAgent/Phoenix необходимо проверять
командой validate-only на доступном экземпляре.
