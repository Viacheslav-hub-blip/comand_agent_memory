# Расширение проекта

Есть три независимые точки подключения. Пользовательский Python-пакет должен
устанавливаться в окружение конвейера. JSON явно перечисляет модули регистрации:

```json
"extensions": ["my_integration.memory_plugin"]
```

Импорт выполняется и при validate-only. Не запускайте модели, запись или запросы
к внешним системам при импорте; делайте это в методах адаптера.

## Источник истории

Пример целиком, читающий экспорт неизвестного агента в JSON-массиве:

```python
"""История внешнего агента из JSON.

Класс ExportHistory: __init__ читает экспорт; list_sessions возвращает сессии;
iter_events выдаёт сообщения выбранной сессии. Других функций нет.
"""
import json
from pathlib import Path

from command_agent_memory.history_sources.conversation_history import HistoryEvent, register_history_source


class ExportHistory:
    """Принимает настройки экспорта и каталог; предоставляет сессии и события."""

    def __init__(self, options: dict, base: Path):
        """Принимает path относительно base; читает JSON-массив сообщений, возвращает None."""
        self.records = json.loads((base / options["path"]).read_text(encoding="utf-8"))

    def list_sessions(self) -> list[dict]:
        """Без входов возвращает уникальные строковые идентификаторы сессий в порядке экспорта."""
        return [{"id": value} for value in dict.fromkeys(str(row["chat_id"]) for row in self.records)]

    def iter_events(self, session: dict):
        """Принимает сессию; выдаёт её сообщения, сохраняя все исходные поля."""
        for number, row in enumerate(self.records):
            if str(row["chat_id"]) == session["id"]:
                yield HistoryEvent("export", session["id"], str(number), "message", row)


register_history_source("export", ExportHistory)
```

Конфигурация источника: `{"type":"export","path":"history.json"}`.
Реальный адаптер должен проверять формат своего экспорта и возвращать понятные
ошибки. Методы не должны изменять историю. Не возвращайте пустые или повторные id.

## Исполнитель модели или агента

Импортируйте `ModelAdapter` и `register_model_adapter` из
`command_agent_memory.model_execution.provider_registry`. Фабрика получает `Path | None` исполняемого файла
и возвращает `ModelAdapter(name, executable, agent_runner, chat_runner)`.

Оба runner получают аргументы общей сигнатуры:

```python
def run(executable: str, model: str, prompt: str, cwd: Path,
        prefix: Path, writable: bool = False) -> str:
    """Принимает параметры агента; возвращает итоговый текст либо вызывает исключение."""
    raise NotImplementedError("Реализуйте вызов SDK или CLI вашей системы.")
```

`cwd` — физическая рабочая область, `prefix` — префикс файлов журнала.
`agent_runner` должен предоставлять модели чтение файлов и при writable=True
запись в переданную рабочую копию. `chat_runner` не должен править рабочие файлы.
Сохраняйте prompt, ответ и доступные измерения под `prefix`. Неизвестные измерения
не заменяйте фиктивными нулями. Секреты берите из окружения.

Регистрация: `register_model_adapter("my_agent", factory)`.
Выбор: `"defaults":{"provider":"my_agent","model":"my-model"}`.
Имена встроенных адаптеров перезаписывать нельзя.

## Хранилище памяти

Контракт находится в `command_agent_memory.memory_storage.storage_registry.MemoryStore`.
Фабрика `(options: dict, base: Path, layout: MemoryLayout) -> MemoryStore`
регистрируется через `register_memory_store("my_store", factory)`.

| Элемент | Требование |
| --- | --- |
| `identity: str` | Стабильный идентификатор области, без DSN/токенов |
| `protected_paths: tuple[Path, ...]` | Абсолютные локальные пути данных; пустой кортеж для удалённой системы |
| `snapshot() -> dict[str, bytes]` | Полный снимок выбранных layout документов без изменения системы |
| `publish(before, after, run) -> None` | Проверка текущей версии по before и публикация after либо ошибка |

Имена документов — безопасные относительные пути в формате POSIX.
Проверяйте их через `layout.allows(name)`; не сохраняйте документы вне layout.
Проверка версии и запись должны выполняться в одной транзакции или через
условное обновление версии на сервере. При конфликте конвейер должен получить
исключение, а память — остаться неизменной. Сохраняйте резервную копию before
в журнале run. Создание адаптера и snapshot не должны создавать БД/таблицы.

В JSON `memory.type` выбирает имя, `personal` и `knowledge` задают layout;
остальные параметры проверяет ваша фабрика. Например:

```json
"memory": {
  "type": "my_store",
  "collection": "alice-memory",
  "token_env": "MEMORY_TOKEN",
  "personal": ["AGENTS.md"],
  "knowledge": ["notes"]
}
```

Образец реализации транзакций —
[`SQLiteMemoryStore`](../command_agent_memory/memory_storage/sqlite_storage.py).
Реализация с резервной копией файлов —
[`FileMemoryStore`](../command_agent_memory/memory_storage/file_storage.py).
Для чтения памяти самим рабочим агентом используйте его SDK или выберите
файловый backend с привычным ему файлом инструкций.

## Границы модулей

- memory_pipeline управляет чтением, двумя анализами, консолидацией, ревью и публикацией.
- application_config проверяет настройки; model_execution.stage_runner выполняет модельные этапы.
- history_sources получает события; history_analysis извлекает контекст и процедуры.
- memory_update готовит и проверяет изменения; memory_storage читает и сохраняет документы.
- team_memory_delivery доставляет опубликованную локальную командную память.
- memory_replay выполняет диалог и сравнение при запрошенной проверке.
- run_reports оформляет отчёты этапов; history_analysis.analysis_reports проверяет версии анализов.

Функция run_pipeline(path, validate_only=False, evaluate_by_replay=False)
находится в command_agent_memory.memory_pipeline. Это программная точка входа.
Схема полного цикла приведена в README. Встроенные скилы хранятся в
`command_agent_memory/skills/<skill-name>/SKILL.md` и включаются в wheel как данные
пакета вместе с собственными `references/`. Поле `name` совпадает с именем
каталога и использует строчные буквы и дефисы. Для получения пути вызывайте
`get_skill_path(name, resource="SKILL.md")` из `command_agent_memory.skill_resources`.

Локальные блокировки предотвращают одновременную обработку одним окружением.
Для удалённого хранилища дополнительно нужна серверная транзакция или проверка
версии при записи: локальная блокировка не синхронизирует разные компьютеры.
