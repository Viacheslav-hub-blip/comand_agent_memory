"""Каталоги запусков и отчёты этапов обработки.

Функции: new_run_id — уникальный ID; create_run_directory — новый каталог;
write_stage_report — Markdown-отчёт, diff, индекс и сводка состояния запуска.
"""
from datetime import datetime, timezone
from difflib import unified_diff
import json
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
