"""Запуск конвейера по конфигурации пользователя.

Функция main принимает аргументы CLI и запускает выбранный режим.
"""
import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys

from command_agent_memory.memory_pipeline import run_pipeline


def main(argv: list[str] | None = None) -> None:
    """Принимает аргументы; запускает конвейер и при запросе печатает один итоговый JSON."""
    parser = argparse.ArgumentParser(description="Личная и командная память агентов")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--evaluate-by-replay", action="store_true")
    modes.add_argument("--team-process", action="store_const", const="process", dest="team_mode")
    modes.add_argument("--team-sync", action="store_const", const="sync", dest="team_mode")
    modes.add_argument("--team-status", action="store_const", const="status", dest="team_mode")
    args = parser.parse_args(argv)
    options = {"validate_only": args.validate_only, "evaluate_by_replay": args.evaluate_by_replay}
    if args.team_mode:
        options["team_mode"] = args.team_mode
    if not args.json:
        result = run_pipeline(args.config, **options)
        if args.team_mode == "status" and not args.validate_only:
            print(json.dumps(result["team"], ensure_ascii=False, indent=2))
        return
    try:
        with redirect_stdout(sys.stderr):
            result = run_pipeline(args.config, raise_on_failure=False, **options)
    except Exception as error:
        result = {"state": "failed", "error": str(error), "users": [], "team": None}
    print(json.dumps(result, ensure_ascii=True))
    if result["state"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
