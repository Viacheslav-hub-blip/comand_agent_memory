# Тестовая команда «Север»

Четыре участника: Анна 101, Борис 102, Вера 103, Денис 104. У каждого два
диалога; восемь сценариев содержат по четыре реплики.

```powershell
python run_synthetic_dialogues.py --check
python run_synthetic_dialogues.py
```

--check не создаёт агента и не вызывает модели. Полный запуск использует
agent_runtime.create_agent и run_turn, настройки OpenRouter из settings.py.
У каждого диалога отдельный агент. Сообщения пользователя, ответы и инструменты
показываются в терминале и сохраняются в SQLite. При сбое прогон останавливается,
полученные сообщения остаются записанными.

Результаты: data/synthetic-team-runs/<run_id>/sessions.json и
users/<id>/user_workspace/. Каждый участник получает свою память и базу.
Исходные материалы копируются из user_workspace/synthetic_team/ без outputs.

## Анализ существующего запуска

```powershell
$annaWorkspace = "data/synthetic-team-runs/20261020T040159Z-a0d71f0d/users/101/user_workspace"
python analyze_chats_codex.py --user-id 101 --workspace $annaWorkspace
python consolidate_reports_codex.py --user-id 101 --workspace $annaWorkspace --stage personal
python consolidate_reports_codex.py --user-id 101 --workspace $annaWorkspace --stage knowledge
```

Для нового run_id замените путь; скрипты не выбирают запуск автоматически.
Для другого участника измените user-id и путь. Общий каталог knowledge/
один для команды; командные записи выполняются последовательно.

## Материалы

- [Участники и порядок диалогов](team-and-schedule.md).
- [Исследование сценариев](research.md).
- [Восемь диалогов](chats/).
- [Контрольные запросы](holdout.md).
- [Эталон проверяющего](expected-results.md).

expected-results.md, reference_queries.sql и validation.json доступны только
проверяющему. Их нельзя добавлять агенту как подсказки для решения задачи.
Общие знания пока не подключены к основному агенту; контроль применения
командного опыта требует этого подключения.
