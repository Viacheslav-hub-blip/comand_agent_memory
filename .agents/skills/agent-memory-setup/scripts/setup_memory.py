"""Проверка и сохранение подключения к истории агентного клиента.

Функции: discover предлагает конфигурацию, check проверяет чтение сессий,
save_config сохраняет новый конфиг, operate выполняет команду, main обслуживает CLI.
"""
import argparse
import importlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


SCRIPTS = Path(__file__).resolve().parent
RUNTIME = SCRIPTS / "io_lib"
sys.path.insert(0, str(RUNTIME if RUNTIME.is_dir() else SCRIPTS.parents[3]))
from command_agent_memory.application_config import read_configuration
from command_agent_memory.connections import team_connection, validate_users
from command_agent_memory.history_sources.project_sessions import DEFAULT_RUNS


def discover(host: str, source_kind: str | None, history: Path | None) -> dict:
    """Принимает клиент, тип и путь истории, возвращает проектные настройки личной памяти."""
    kind = source_kind or (host if host in ("codex", "claude", "gigaagent") else "jsonl")
    source = {"type": kind}
    if history is not None:
        source["path"] = str(history)
    if kind in ("codex", "claude"):
        source["latest_in_project"] = "."
        if kind == "claude":
            source["include_subagents"] = False
    elif history is None:
        if kind == "gigaagent":
            raise ValueError("Укажите JSON-экспорт GigaAgent или настройте источник с url и thread_ids.")
        raise ValueError("Укажите путь к экспорту истории JSONL.")
    personal = {"claude": "CLAUDE.md", "gigacode": "GIGACODE.md"}.get(host, "AGENTS.md")
    return {"users": [{
        "user_id": "local",
        "workspace": DEFAULT_RUNS,
        "source": source,
        "memory": {"type": "files", "root": ".", "personal": [personal],
                   "knowledge": [".agent-memory/knowledge"]},
    }]}


def check(settings: dict, project: Path) -> list[dict]:
    """Принимает конфиг и корень проекта, возвращает метаданные прочитанных сессий."""
    extensions = settings.get("extensions", [])
    if not isinstance(extensions, list) or any(not isinstance(name, str) or not name for name in extensions):
        raise ValueError("extensions должен содержать имена Python-модулей.")
    for name in extensions:
        importlib.import_module(name)
    users = validate_users(settings, project)
    team_connection(settings.get("team"), project, users)
    result = []
    for user in users:
        sessions = []
        for session in user["sessions"]:
            events = sum(1 for _ in user["source"].iter_events(session))
            if not events:
                raise ValueError(f"Пустая сессия {session['id']}.")
            sessions.append({"id": session["id"], "events": events})
        if not sessions:
            raise ValueError(f"Источник участника {user['user_id']} не вернул сессий.")
        source = user["source_options"]
        result.append({"user_id": user["user_id"], "type": source["type"],
                       "path": source.get("path") or user["sessions"][0].get("path"),
                       "url": source.get("url"), "sessions": sessions})
    return result


def save_config(path: Path, settings: dict) -> None:
    """Принимает итоговый путь и конфиг, сохраняет его целиком при свободном имени."""
    with TemporaryDirectory(prefix=".memory-setup-", dir=path.parent) as directory:
        temporary = Path(directory) / path.name
        contents = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
        temporary.write_text(contents, encoding="utf-8")
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise FileExistsError(f"Конфиг уже существует: {path}. Проверьте его командой check.") from error


def operate(args: argparse.Namespace) -> dict:
    """Принимает аргументы команды, возвращает результат проверки и при запросе новый конфиг."""
    project = args.project.resolve(strict=True)
    if not project.is_dir():
        raise ValueError("--project должен указывать на каталог проекта.")
    destination = project / "memory_pipeline.json"
    if args.action == "discover":
        settings = discover(args.host, args.source, args.history)
        return {"state": "validated", "configuration": settings, "users": check(settings, project)}
    configuration = args.config or destination
    settings = read_configuration(project / configuration)
    users = check(settings, project)
    if args.action == "save":
        save_config(destination, settings)
        return {"state": "saved", "config": str(destination), "users": users}
    return {"state": "validated", "config": str(project / configuration), "users": users}


def main() -> int:
    """Читает параметры CLI, печатает итоговый JSON и возвращает код завершения."""
    parser = argparse.ArgumentParser(description="Настройка источника истории для Agent Memory.")
    parser.add_argument("action", choices=("discover", "check", "save"))
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--host")
    parser.add_argument("--source", choices=("codex", "claude", "jsonl", "gigaagent"))
    parser.add_argument("--history", type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.action == "discover" and not args.host:
        parser.error("Для discover нужен --host.")
    if args.action == "save" and args.config is None:
        parser.error("Для save нужен --config проверяемого кандидата.")
    try:
        result = operate(args)
    except Exception as error:
        print(json.dumps({"state": "failed", "error": str(error)}, ensure_ascii=True))
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
