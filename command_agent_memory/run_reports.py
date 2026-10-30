"""Каталоги запусков и отчёты этапов.

Функции: new_run_id — уникальный ID; create_run_directory — новый каталог;
write_stage_report — Markdown, diff и индекс отчётов одного запуска;
read_reports — полные аналитические отчёты; report_fingerprint — версия входов.
"""
from datetime import datetime, timezone
from difflib import unified_diff
import json
import hashlib
from pathlib import Path
from uuid import uuid4


def new_run_id(moment: datetime | None = None) -> str:
    """Принимает необязательное время; возвращает уникальный сортируемый ID запуска."""
    current = moment or datetime.now()
    return current.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:12]


def create_run_directory(parent: Path, run_id: str | None = None) -> Path:
    """Принимает родительский каталог и ID; создаёт новый каталог и возвращает путь.

    Существующий каталог не переиспользуется: ``FileExistsError`` защищает
    артефакты предыдущих запусков от случайной перезаписи.
    """
    directory = parent.resolve() / (run_id or new_run_id())
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def write_stage_report(run: Path, name: str, title: str, state: str, summary: str,
                       before: dict[str, bytes] | None = None,
                       after: dict[str, bytes] | None = None,
                       links: dict[str, Path] | None = None) -> Path:
    """Принимает сведения этапа; записывает отчёт, diff и общий индекс, возвращает путь отчёта.

    Снимки сравниваются по байтам для списка файлов, без анализа их смысла.
    Повторная запись обновляет состояние того же этапа, остальные отчёты сохраняются.
    """
    directory = run / "report"
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / f"{name}.md"
    lines = [f"# {title}", "", f"Состояние: {state}", "", summary]
    if before is not None and after is not None:
        added = sorted(after.keys() - before.keys())
        removed = sorted(before.keys() - after.keys())
        changed = sorted(path for path in before.keys() & after.keys() if before[path] != after[path])
        lines.extend(["", "## Файлы"])
        for label, paths in (("Добавлены", added), ("Изменены", changed), ("Удалены", removed)):
            if paths:
                lines.extend(["", f"{label}:", "", *(f"- {path}" for path in paths)])
        if not (added or removed or changed):
            lines.extend(["", "Изменений файлов нет."])
        diff = directory / f"{name}.diff"
        parts = []
        for path in sorted(set(added + removed + changed)):
            difference = unified_diff(
                before.get(path, b"").decode("utf-8", errors="replace").splitlines(keepends=True),
                after.get(path, b"").decode("utf-8", errors="replace").splitlines(keepends=True),
                fromfile=f"before/{path}", tofile=f"after/{path}",
            )
            for line in difference:
                parts.append(line)
                if not line.endswith("\n"):
                    parts.append("\n\\ No newline at end of file\n")
        diff.write_text("".join(parts), encoding="utf-8")
        lines.extend(["", f"[Различия файлов]({diff.name})"])
    if links:
        lines.extend(["", "## Материалы", ""])
        lines.extend(f"- [{label}]({path.resolve().as_posix()})" for label, path in links.items())
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    index_path = directory / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    index[name] = {"title": title, "state": state, "file": report.name, "summary": summary,
                   "updated_at": datetime.now(timezone.utc).isoformat()}
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    overview = ["# Состояние запуска", "", "Отчёты обновляются после каждого этапа.", ""]
    for key in ("publication", "replay"):
        if key in index:
            overview.extend([index[key]["summary"], ""])
    overview.extend(f"- [{entry['title']}]({entry['file']}): {entry['state']}" for entry in index.values())
    (directory / "summary.md").write_text("\n".join(overview) + "\n", encoding="utf-8")
    return report


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
