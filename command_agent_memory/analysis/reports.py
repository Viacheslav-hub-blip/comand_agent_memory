"""Техническая версия общего хранилища аналитических отчётов пользователя.

Вход: каталог отчётов. Выход: SHA-256 путей и полного содержимого файлов.
Функция report_fingerprint определяет изменение входов без разбора Markdown.
"""
import hashlib
from pathlib import Path


def report_fingerprint(directory: Path) -> str:
    """Принимает каталог; возвращает хеш пар отчётов, manifest и прежних .md-файлов.

    Версия служит для повторного запуска обработки, а не подтверждением знаний.
    Отсутствие каталога вызывает ValueError; содержимое отчётов не классифицируется.
    """
    if not directory.is_dir():
        raise ValueError(f"Нет каталога отчётов: {directory}")
    digest = hashlib.sha256()
    for session in sorted(directory.glob("session-*")):
        paths = [session] if session.is_file() and session.suffix == ".md" else [
            session / name for name in ("context.md", "procedures.md", "manifest.json")
        ] if session.is_dir() else []
        for path in paths:
            if path.is_file():
                digest.update(path.relative_to(directory).as_posix().encode("utf-8") + b"\0")
                digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()
