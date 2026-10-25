"""Чтение отчётов целиком и выгрузка истории одного диалога.

Вход: каталог отчётов либо SQLite, идентификаторы пользователя и сеанса.
Выход: тексты отчётов контекста и решений либо JSONL-история всех сообщений.
Функции: read_reports — чтение файлов без разбора Markdown;
export_chat_history — выгрузка диалога из SQLite только для чтения.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parent


def read_reports(directory: Path) -> tuple[str, str, int]:
    """Принимает каталог; возвращает тексты контекста, решений и число диалогов.

    Читает session-ID/context.md и procedures.md целиком, без разбора разделов.
    Старый session-ID.md передаётся целиком как контекст, если нет новой пары.
    Служебный manifest не позволяет смешать результаты незавершённого запуска.
    Ошибки чтения файлов передаются вызывающему коду.
    """
    contexts, procedures = [], []
    count = 0
    for path in sorted(directory.glob("session-*")):
        if path.is_dir():
            manifest = path / "manifest.json"
            if manifest.exists() and json.loads(
                manifest.read_text(encoding="utf-8")
            )["state"] != "completed":
                raise RuntimeError(f"Анализ диалога не завершён: {path}")
            contexts.append((path / "context.md").read_text(encoding="utf-8-sig"))
            procedures.append((path / "procedures.md").read_text(encoding="utf-8-sig"))
        elif path.suffix == ".md" and not path.with_suffix("").is_dir():
            contexts.append(path.read_text(encoding="utf-8-sig"))
        else:
            continue
        count += 1
    return "\n\n".join(contexts), "\n\n".join(procedures), count


def export_chat_history(database_path: Path, user_id: str, session_id: str,
                        output_path: Path) -> int:
    """Выгружает сообщения одного диалога в JSONL без повторения content.

    Вход: путь SQLite, идентификаторы пользователя и сеанса, путь выходного файла.
    Выход: UTF-8-файл с сообщениями по id; возвращает число записанных сообщений.
    База открывается только для чтения. Сохраняются все поля message_json;
    content записывается отдельно, если отличается или JSON не содержит его.
    """
    count = 0
    with closing(sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            "SELECT id, role, content, message_json FROM messages "
            "WHERE user_id = ? AND session_id = ? ORDER BY id", (user_id, session_id),
        )
        with output_path.open("w", encoding="utf-8") as history:
            for message_id, role, content, raw_message in rows:
                try:
                    message = json.loads(raw_message)
                except (TypeError, ValueError):
                    message = raw_message
                record = {"id": message_id, "role": role, "message": message}
                payload = message.get("data", message) if isinstance(message, dict) else message
                if not isinstance(payload, dict) or payload.get("content") != content:
                    record["content"] = content
                history.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
    return count
