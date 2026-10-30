# Пример диалогов команды «Север»

Четыре участника: Анна 101, Борис 102, Вера 103, Денис 104. У каждого два
диалога; восемь сценариев содержат по четыре исходные пользовательские реплики.
Материалы используются как примеры истории и сценариев проверки памяти.

Для обработки сохранённых диалогов задайте их источник в JSON-конфигурации:
`source.type: sqlite` с SELECT по вашей таблице либо `source.type: jsonl`.
Укажите отдельные workspace и memory каждого пользователя и запустите:

```powershell
python -m command_agent_memory --config memory_pipeline.json --validate-only
python -m command_agent_memory --config memory_pipeline.json
```

[Настройка источников](../../configuring_agents_and_databases.md).
Для проверки по исходным репликам используется [диалоговый сценарий Анны](../reconciliation_replay/dialogue_scenario.json).

## Материалы

- [Участники и порядок диалогов](team-and-schedule.md).
- [Исследование сценариев](research.md).
- [Восемь диалогов](chats/).
- [Контрольные запросы](holdout.md).
- [Эталон проверяющего](expected-results.md).

expected-results.md, reference_queries.sql и validation.json предназначены
проверяющему. Их нельзя передавать решающему агенту как подсказки.
