# Командная память через Git

Руководство описывает конфигурацию отправителя, исполнителя и получателя.
Функций и классов в документе нет.

Используйте отдельный репозиторий для одной команды. Участники с доступом к Git могут
читать ветки вкладов и служебный реестр. В `team_knowledge/` доставляются
только действующий индекс и принятые документы. Права на каноническую ветку
задаёт хостинг. SDK использует штатную аутентификацию Git через credential
helper или SSH.

## Быстрый запуск

Для первой проверки создайте отдельный локальный bare-репозиторий с пустым
начальным commit в `main`. Выполните команды в новом тестовом каталоге.

```powershell
git init --bare --initial-branch=main team-memory.git
git init --initial-branch=main seed
git -C seed -c user.name="Memory fixture" -c user.email=memory@example.test commit --allow-empty -m "Initialize team memory"
git -C seed push ../team-memory.git main
```

Добавьте к личной конфигурации поле `team` со следующим объектом.
Пути считаются от файла конфигурации. В `export_paths` указываются пути из **личной** принятой памяти.
Корневой префикс `knowledge/` при экспорте снимается.

```json
{
  "type": "git",
  "team_id": "example-team",
  "repository": "./team-memory.git",
  "branch": "main",
  "cache_dir": "./member-cache",
  "export_paths": ["knowledge/wiki", "knowledge/cases", "knowledge/skills"]
}
```

Обычный запуск обновляет личную память и отправляет выбранные принятые файлы.
Из уже принятых результатов формируется тот же идентификатор вклада.
Очередь хранится в `cache_dir/outbox/`. При повторном запуске отправляются
пакеты, которые ещё не подтверждены чтением удалённой ветки.

Исполнителю нужен свой конфиг с настройками модели и тем же `team_id`,
`repository`, `branch`. Он использует собственный `cache_dir`, пустой
`export_paths`, поле `users` можно опустить. Получателю достаточно Git-блока
с отдельно заданным кэшем, пустым `export_paths` и
`"workspaces": ["./recipient"]`. Корень получателя должен существовать.

```powershell
python -m command_agent_memory --config member.json --json
python -m command_agent_memory --config executor.json --team-process --json
python -m command_agent_memory --config recipient.json --team-sync --json
```

Любой из режимов можно сочетать с `--validate-only` для проверки локальной
конфигурации без моделей и подключения к remote. Git-режимы обработки и
синхронизации используют тот же CLI. Отправка, обработка и доставка имеют
отдельные статусы. При отказе командной модели квитанция содержит
`needs_resolution`, и командные документы сохраняются. При недоступности
remote получатель сообщает `cached` и SHA последнего проверенного снимка,
если такой снимок уже есть. Поле `remote_error` объясняет, почему использован кэш.

Установленный скилл принимает те же три конфигурации. Укажите нужную роль
и путь к конфигурации в запросе. Модельные задачи отправителя и исполнителя
выполняются штатными субагентами, получатель читает Git штатными командами.
[Режимы скилла](../.agents/skills/update-agent-memory/references/modes.md).

[Формат пакета, квитанций и кэша](../specifications/distributed_team_memory.md).

## Фоновый исполнитель на Windows

Назначьте одну машину и одну учётную запись исполнителем команды. Установите wheel
в постоянное виртуальное окружение. Для stage team должны быть доступны модель,
провайдер и права Git на запись в каноническую ветку. У участников свои кэши и
права на ветки вкладов. Для клиентов на разных машинах задайте общий Git remote.

Скопируйте [пример конфига](examples/team-executor.example.json) в `executor.json`,
задайте team_id, repository, модель и отдельный cache_dir. В активном окружении
выполните:

```powershell
command-agent-memory-worker --config executor.json --install
Get-ScheduledTaskInfo -TaskName CommandAgentMemory-example-team
command-agent-memory --config member.json --team-status --json
```

Установщик регистрирует в Планировщике Windows задание текущего пользователя.
Первый запуск запланирован через минуту, следующие — каждые пять минут. Интервал
задаётся при установке через `--minutes`. Планировщик работает в пользовательской
сессии, пропускает перекрывающиеся запуски и подхватывает пропущенный запуск после
возврата машины. Удаление задания: `Unregister-ScheduledTask -TaskName CommandAgentMemory-example-team`.
Для другой ОС планировщику передайте ту же команду без `--install`. Журнал последнего
запуска — `cache_dir/last-worker-run.json`.

Статус отправителя читается по его outbox, веткам и квитанциям на указанном commit.
`queued` — вклад ещё локален, `awaiting_processing` — ожидает исполнителя,
`applied` и `unchanged` — получена квитанция обработки. Поле knowledge показывает
связанные с вкладом актуальные записи, их accepted/pending/conflicts и reason.
`needs_resolution` означает отказ от применения всего вклада; новые квитанции могут
содержать пояснение resolution. `missing_remote` означает исчезновение ранее
доставленной ветки без квитанции. Для автономных скиллов та же операция — `io team-status --config C`.

## Использование снимка

Штатное приложение может держать блокировку доставленного снимка на один
запрос через `create_team_session`. Сессия проверяет хеши файлов и открывает
командный каталог для штатных файловых инструментов в режиме чтения.
При выходе сверяется, что во время задачи снимок не менялся.
Каталог, доставленный прежней версией проекта, достаточно обновить
`--team-sync`. Синхронизация допишет хеши файлов к существующей метке.

```python
from command_agent_memory.working_agent import create_team_session

with create_team_session(workspace, {"model": model_name}) as (session, commit):
    for event in session.agent.stream(
        {"messages": [("user", request)]},
        config={"configurable": {"thread_id": thread_id}},
    ):
        process_event(event)
```

Для своего раннера используйте `read_team_version(workspace)` из
`command_agent_memory.team_memory_delivery.git_team` как контекст на время задачи.
Контекст возвращает SHA и проверяет снимок без зависимости от DeepAgents.
Агент может читать `team_knowledge/INDEX.md` способом, который уже использует
для локальной памяти. При чтении файлов напрямую без этого контекста запускайте
синхронизацию между задачами. [Подключение собственного агента](adding_integrations.md).

Транспорт проверяет документы UTF-8 на известные признаки секретов и локальных
путей. Заполненные поля учётных данных внутри JSON отклоняются, включая вложенные
объекты. Содержимое для экспорта по-прежнему задаётся через `export_paths`.
