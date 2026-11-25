"""Запуск памяти с модельными заданиями для агента, вызвавшего скилл.

Функции: repository_path находит код, native_configuration выбирает исполнителя,
run выполняет конвейер, main разбирает аргументы.
"""
import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys


def repository_path() -> Path:
    """Без входов возвращает каталог с кодом из комплекта или исходного репозитория."""
    script = Path(__file__).resolve()
    bundled = script.parent / "runtime"
    for candidate in (bundled, *script.parents):
        if (candidate / "command_agent_memory" / "memory_pipeline.py").is_file():
            return candidate
    raise FileNotFoundError("Скопируйте весь комплект скилла с каталогом scripts/runtime.")


def native_configuration(configuration: dict, model: str) -> dict:
    """Принимает настройки и модель хоста; сохраняет данные и переключает модельные этапы на задания."""
    stages = configuration.get("stages", {})
    if not isinstance(stages, dict) or any(not isinstance(stage, dict) for stage in stages.values()):
        raise ValueError("stages должен содержать объекты настроек этапов.")
    if any("max_model_calls" in stage for stage in stages.values()):
        raise ValueError("В режиме субагентов используйте timeout_seconds. Число вызовов контролирует агент-хост.")
    return {**configuration, "defaults": {"provider": "native", "model": model}, "stages": {
        name: {key: value for key, value in stage.items() if key not in {"provider", "model", "executable"}}
        for name, stage in stages.items()
    }}


def run(args: argparse.Namespace) -> int:
    """Принимает аргументы запуска; печатает итоговый JSON и возвращает код завершения."""
    from command_agent_memory.history_sources.project_sessions import DEFAULT_RUNS, default_configuration, protect_local_runs
    from command_agent_memory.application_config import read_configuration, resolve_path
    from command_agent_memory.memory_pipeline import run_pipeline
    from command_agent_memory.memory_replay.host_replay import HostReplay
    from command_agent_memory.model_execution.host_tasks import HostTasks
    from command_agent_memory.model_execution.provider_registry import register_model_adapter

    try:
        project = args.project.resolve(strict=True)
        if not project.is_dir():
            raise ValueError("--project должен указывать на каталог проекта.")
        path = (project / args.config).resolve() if args.config else project / "memory_pipeline.json"
        if args.config or (path.is_file() and args.history is None and args.source is None and args.session is None):
            configuration = read_configuration(path, team_only=args.team_mode is not None)
        else:
            configuration = default_configuration(project, args.host, args.source, args.history, args.session)
        users = configuration["users"]
        if args.scenario is not None and (not args.evaluate_by_replay or len(users) != 1):
            raise ValueError("--scenario используется для replay одного участника.")
        if args.evaluate_by_replay and len(users) == 1:
            user, = users
            scenario = args.scenario
            default_scenario = project / "replay_scenario.json"
            if scenario is None and not user.get("replay_scenario") and default_scenario.is_file():
                scenario = default_scenario
            if scenario is not None:
                configuration["users"] = [{**user, "replay_scenario": str((project / scenario).resolve())}]
        configuration = native_configuration(configuration, args.model)
        if not args.validate_only and args.tasks is None:
            raise ValueError("Для исполнения нужен новый каталог заданий (--tasks).")
        bridge = HostTasks(args.tasks or project)
        register_model_adapter("native", bridge.adapter)
        options = {"configuration": configuration, "evaluate_by_replay": args.evaluate_by_replay,
                   "team_mode": args.team_mode, "raise_on_failure": False}
        if args.evaluate_by_replay:
            if args.host != "codex":
                raise ValueError("Штатные проверочные сессии replay сейчас подключены для Codex.")
            options["replay_factory"] = HostReplay(bridge, args.model).create
        with redirect_stdout(sys.stderr):
            output = run_pipeline(path, validate_only=True, **options)
            if not args.validate_only:
                default_runs = project / DEFAULT_RUNS
                uses_default_runs = args.team_mode is None and any(
                    resolve_path(path.parent, user["workspace"]) == default_runs for user in users
                )
                if uses_default_runs or default_runs in bridge.directory.parents:
                    protect_local_runs(project)
                bridge.directory.mkdir(parents=True)
                output = run_pipeline(path, **options)
    except Exception as error:
        output = {"state": "failed", "error": str(error), "users": [], "team": None}
    print(json.dumps(output, ensure_ascii=True), flush=True)
    return int(output["state"] == "failed")


def main() -> int:
    """Без аргументов функции читает командную строку; запускает конвейер или принимает ответ."""
    parser = argparse.ArgumentParser(description="Обновление памяти через родного агента.")
    modes = parser.add_subparsers(dest="command", required=True)
    execute = modes.add_parser("run")
    execute.add_argument("--project", type=Path, default=Path.cwd())
    execute.add_argument("--host", required=True)
    execute.add_argument("--model", default="host-default")
    execute.add_argument("--config", type=Path)
    execute.add_argument("--source", choices=("codex", "claude", "jsonl"))
    execute.add_argument("--history", type=Path)
    execute.add_argument("--session")
    execute.add_argument("--scenario", type=Path)
    execute.add_argument("--tasks", type=Path)
    execute.add_argument("--validate-only", action="store_true")
    pipeline = execute.add_mutually_exclusive_group()
    pipeline.add_argument("--evaluate-by-replay", action="store_true")
    pipeline.add_argument("--team-process", action="store_const", const="process", dest="team_mode")
    pipeline.add_argument("--team-sync", action="store_const", const="sync", dest="team_mode")
    pipeline.add_argument("--team-status", action="store_const", const="status", dest="team_mode")
    collect = modes.add_parser("reply")
    collect.add_argument("task", type=Path)
    reply = collect.add_mutually_exclusive_group(required=True)
    reply.add_argument("--answer", type=Path)
    reply.add_argument("--error")
    args = parser.parse_args()
    sys.path.insert(0, str(repository_path()))
    if args.command == "run":
        return run(args)
    from command_agent_memory.model_execution.host_tasks import save_reply

    try:
        save_reply(args.task, args.answer, args.error)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
