"""Выбор последнего диалога проекта для двух поставок Agent Skills.

Функции: default_history_path возвращает штатный каталог, discover_project_sessions
находит диалоги проекта, resolve_project_source выбирает свежую сессию из сохранённых
настроек, default_configuration создаёт настройки одного запуска,
protect_local_runs исключает журналы из Git.
"""
import os
from pathlib import Path
import sys


DEFAULT_RUNS = ".agent-memory/runs"


def default_history_path(kind: str) -> Path:
    """Принимает тип Codex или Claude Code и возвращает штатный каталог в текущем окружении."""
    if kind == "codex":
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    if kind == "claude":
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    raise ValueError("Укажите путь к истории или подготовьте экспорт JSONL.")


def discover_project_sessions(source: dict, project: Path) -> list[dict]:
    """Принимает Codex/Claude-источник и проект; возвращает его чаты от старого к новому."""
    from command_agent_memory.history_sources.conversation_history import load_history_source

    history = Path(source["path"])
    reader = load_history_source(source, project)
    if history.is_file():
        sessions = reader.list_sessions()
        return [item for item in sessions if Path(item["cwd"]).resolve() == project]
    matches = []
    for path in reader.paths():
        try:
            current = load_history_source({**source, "path": str(path)}, project).list_sessions()
            for item in current:
                if Path(item["cwd"]).resolve() == project:
                    matches.append((path.stat().st_mtime_ns, item["id"], item))
        except (ValueError, OSError, RuntimeError) as error:
            print(f"Пропущен недоступный заголовок истории {path}: {error}", file=sys.stderr)
    return [item for _, _, item in sorted(matches, key=lambda entry: entry[:2])]


def resolve_project_source(source: dict, base: Path) -> dict:
    """Принимает настройки и каталог конфига, возвращает конкретный источник для текущего запуска."""
    if not isinstance(source, dict) or "latest_in_project" not in source:
        return source
    from command_agent_memory.application_config import resolve_path

    options = dict(source)
    project = resolve_path(base, options.pop("latest_in_project")).resolve(strict=True)
    kind = options.get("type")
    if kind not in ("codex", "claude") or {"session_ids", "projects", "limit"} & options.keys():
        raise ValueError("latest_in_project поддерживает Codex и Claude Code без дополнительных фильтров сессий.")
    path = options.get("path", str(default_history_path(kind)))
    options["path"] = str(resolve_path(base, path))
    sessions = discover_project_sessions(options, project)
    if not sessions:
        raise ValueError(f"В {options['path']} нет сессий проекта {project}.")
    return {**options, "path": sessions[-1]["path"], "session_ids": [sessions[-1]["id"]]}


def default_configuration(project: Path, host: str, source_kind: str | None,
                          history: Path | None, session_id: str | None) -> dict:
    """Принимает проект, тип агента и параметры истории; возвращает настройки последней сессии проекта."""
    from command_agent_memory.history_sources.conversation_history import load_history_source

    kind = source_kind or (host if host in {"codex", "claude"} else "jsonl")
    if history is None:
        history = default_history_path(kind)
    source = {"type": kind, "path": str((project / history.expanduser()).resolve())}
    if kind == "claude":
        source["include_subagents"] = False
    if kind in {"claude", "codex"}:
        sessions = discover_project_sessions(source, project)
    else:
        sessions = load_history_source(source, project).list_sessions()
    if session_id is not None:
        sessions = [item for item in sessions if item["id"] == session_id]
    if not sessions:
        raise ValueError(f"В истории {kind} нет выбранного диалога проекта {project}.")
    if kind != "jsonl":
        source["path"] = sessions[-1]["path"]
    source["session_ids"] = [sessions[-1]["id"]]
    return {"users": [{
        "user_id": "local", "workspace": DEFAULT_RUNS, "source": source,
        "memory": {"type": "files", "root": ".", "personal": ["CLAUDE.md" if host == "claude" else "AGENTS.md"],
                   "knowledge": [".agent-memory/knowledge"]},
    }]}


def protect_local_runs(project: Path) -> None:
    """Добавляет правило Git в каталог журналов проекта. Принимает корень, возвращает None."""
    from command_agent_memory.memory_storage.file_storage import checked_path
    from command_agent_memory.memory_storage.memory_locking import memory_lock

    ignore = checked_path(project, f"{DEFAULT_RUNS}/.gitignore")
    ignore.parent.mkdir(parents=True, exist_ok=True)
    with memory_lock(ignore, wait=True), ignore.open("a+b") as stream:
        stream.seek(0)
        existing = stream.read()
        if existing.splitlines()[-1:] != [b"*"]:
            if existing and not existing.endswith(b"\n"):
                stream.write(b"\n")
            stream.write(b"*\n")
