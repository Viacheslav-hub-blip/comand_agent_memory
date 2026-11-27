# Движение и обработка данных

Схема показывает путь истории до личной и командной памяти и контрольные точки
публикации. Функций и классов в документе нет.

## Личная память

```mermaid
flowchart TD
    Host["Журналы клиента · JSONL · SQL · API"]
    Setup["Поиск или указанный путь<br/>agent-memory-setup"]
    Settings["Проверенное подключение<br/>memory_pipeline.json"]
    Check{"Входы запрошенного режима полны?"}
    Stop["Остановка до вызова моделей"]
    Events["События выбранной сессии<br/>HistoryEvent → JSONL"]
    Context["context-analysis<br/>правила и определения"]
    Procedures["procedure-analysis<br/>случаи и процедуры"]
    Baseline["Действующая память<br/>before/"]
    Draft["Консолидация<br/>draft/"]
    Review{"Принято ревью?"}
    After["Снимок после ревью<br/>after/"]
    Replay{"Запрошен replay?"}
    Evaluate{"Победил candidate?"}
    Keep["Действующая память сохраняется"]
    Store["Запись через MemoryStore<br/>файлы или SQLite"]
    Status["status.json · отчёты и хеши"]
    Host --> Setup --> Settings --> Check
    Check -->|Да| Events
    Check -->|Нет| Stop
    Events --> Context & Procedures
    Context & Procedures & Baseline --> Draft --> Review
    Review -->|Да| After --> Replay
    Review -->|Нет| Keep
    Replay -->|Нет| Store
    Replay -->|Да| Evaluate
    Evaluate -->|Да| Store
    Evaluate -->|Отказ или сбой| Keep
    Store & Keep --> Status
```

Для явно запрошенного replay входы проверяются до модельной обработки.
Ревью и сравнение используют тот же снимок `after/`, который затем записывается.
Перед записью проверяется, что исходная память соответствует `before/`.

| Данные | Где искать |
| --- | --- |
| Исходная история и отчёты сессии | В CLI и мосте — `analysis-runs/` и `raw/chat-reports-*/session-*/`. В автономном скилле — `skill-runs/<id>/personal/history/` и ссылки в `stages.json`. |
| Снимки и итог личного запуска | `consolidation-runs/<id>/personal/` или `skill-runs/<id>/personal/`. Внутри находятся `before/`, `draft/`, `after/` и `status.json`. |
| Действующая память проекта | Пути из `memory.personal` и `memory.knowledge`, для файлового режима обычно `AGENTS.md` или `CLAUDE.md` и `.agent-memory/knowledge/`. |

## Проверка replay

```mermaid
flowchart LR
    Task["Исходные реплики и одинаковые входы"]
    Before["Прежняя память"]
    After["Предложенная память"]
    Old["Свежая сессия baseline"]
    New["Свежая сессия candidate"]
    Ref["Цель и закрытый эталон"]
    Tester["Тестировщик выбирает следующие реплики"]
    Judge["Оценщик сравнивает результаты"]
    Decision["decision.json"]
    Task & Before --> Old
    Task & After --> New
    Ref --> Tester & Judge
    Tester --> Old & New
    Old & New --> Judge --> Decision
```

Перед запуском сравниваются модель, параметры, инструкции, инструменты и среда
двух независимых сессий. Оценщик получает ответы, созданные файлы и доступные
метрики. Публикация требует решения `candidate`.

## Командная память

```mermaid
flowchart TD
    Accepted["Принятая личная память<br/>проверка статуса и хешей"]
    Export["Выбранные знания"]
    Queue["Git-очередь<br/>outbox → contributions/id"]
    Team["Командная консолидация"]
    Registry["Реестр источников и решений"]
    Local["Локальный team.root"]
    Git["Git-коммит: знания + квитанция"]
    Recipient["Получатель<br/>team_knowledge/"]
    Accepted --> Export
    Export -->|Локально| Team
    Export -->|Git| Queue --> Team
    Team --> Registry
    Registry -->|Локально| Local --> Recipient
    Registry -->|Git| Git -->|team-sync| Recipient
```

В `team_knowledge/` попадают действующий индекс и принятые документы.
Записи `pending` и `conflicts` сохраняются в реестре. В Git-режиме получатель
фиксирует SHA доставленного снимка и контрольные суммы файлов.

[Структура проекта](project-structure.md) · [Руководство по replay](../evaluating_memory_with_replay.md) · [Настройка Git-доставки](../git_team_memory.md)
