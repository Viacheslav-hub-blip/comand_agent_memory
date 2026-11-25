"""Чтение завершённых результатов анализа истории и проверка их версии.

Функции: read_reports возвращает контекст, процедуры и число диалогов;
report_fingerprint возвращает хеш содержимого отчётов и manifest.
"""
import hashlib
import json
from pathlib import Path


def read_reports(directory: Path, user_id: str | None = None) -> tuple[str, str, int]:
    """Принимает каталог и владельца при наличии; возвращает два текста и число диалогов.

    Читает session-ID/context.md и procedures.md целиком, без разбора разделов.
    Служебный manifest не позволяет смешать результаты незавершённого запуска.
    Ошибки чтения файлов передаются вызывающему коду.
    """
    contexts, procedures = [], []
    count = 0
    for path in sorted(directory.glob("session-*")):
        if path.is_dir():
            manifest = path / "manifest.json"
            if manifest.exists():
                metadata = json.loads(manifest.read_text(encoding="utf-8"))
                if metadata["state"] != "completed":
                    raise RuntimeError(f"Анализ диалога не завершён: {path}")
                owner = metadata.get("user_id")
                if user_id is not None and owner is not None and str(owner) != str(user_id):
                    raise ValueError(f"Отчёты принадлежат другому пользователю: {path}")
            contexts.append((path / "context.md").read_text(encoding="utf-8-sig"))
            procedures.append((path / "procedures.md").read_text(encoding="utf-8-sig"))
        else:
            continue
        count += 1
    return "\n\n".join(contexts), "\n\n".join(procedures), count


def report_fingerprint(directory: Path) -> str:
    """Принимает каталог; возвращает хеш пар отчётов, manifest.

    Версия служит для повторного запуска обработки, а не подтверждением знаний.
    Отсутствие каталога вызывает ValueError; содержимое отчётов не классифицируется.
    """
    if not directory.is_dir():
        raise ValueError(f"Нет каталога отчётов: {directory}")
    digest = hashlib.sha256()
    for session in sorted(directory.glob("session-*")):
        paths = [
            session / name for name in ("context.md", "procedures.md", "manifest.json")
        ] if session.is_dir() else []
        for path in paths:
            if path.is_file():
                digest.update(path.relative_to(directory).as_posix().encode("utf-8") + b"\0")
                digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()
