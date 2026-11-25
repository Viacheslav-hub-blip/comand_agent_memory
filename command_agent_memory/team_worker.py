"""Установка и однократный запуск планового исполнителя Git-команды.

Функции: install_task регистрирует задание Windows, run_once обрабатывает очередь
и сохраняет состояние, main читает параметры и возвращает код завершения.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from command_agent_memory.application_config import read_configuration
from command_agent_memory.memory_pipeline import run_pipeline
from command_agent_memory.memory_storage.file_storage import checked_path
from command_agent_memory.team_memory_delivery.git_team import GitTeamConfig
from command_agent_memory.team_memory_delivery.contributions import json_bytes, write_atomic


def install_task(config: Path, team: GitTeamConfig, minutes: int) -> None:
    """Принимает путь настроек, команду и интервал; регистрирует задачу текущего пользователя Windows."""
    if sys.platform != "win32" or minutes < 1:
        raise ValueError("Для установки нужны Windows и положительный интервал в минутах.")
    python = Path(sys.executable).with_name("pythonw.exe").resolve(strict=True)
    installer = Path(__file__).with_name("install-team-worker.ps1")
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(installer), "-Config", str(config), "-Python", str(python),
                    "-TeamId", team.team_id, "-Minutes", str(minutes)], check=True)


def run_once(config: Path, team: GitTeamConfig) -> int:
    """Принимает путь настроек и команду; обрабатывает очередь, сохраняет время и итог, возвращает код."""
    started = datetime.now(timezone.utc).isoformat()
    try:
        result = run_pipeline(config, team_mode="process", raise_on_failure=False)
    except Exception as error:
        result = {"state": "failed", "error": str(error)}
    team.cache_dir.mkdir(parents=True, exist_ok=True)
    report = {"started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(), **result}
    write_atomic(checked_path(team.cache_dir, "last-worker-run.json"), json_bytes(report))
    print(json.dumps(report, ensure_ascii=True))
    return int(result["state"] == "failed")


def main() -> int:
    """Читает CLI исполнителя, проверяет конфигурацию Git-команды и возвращает код выбранной операции."""
    parser = argparse.ArgumentParser(description="Исполнитель командной памяти")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--install", action="store_true", help="Установить задание Windows")
    parser.add_argument("--minutes", type=int, default=5)
    args = parser.parse_args()
    config = args.config.resolve(strict=True)
    settings = read_configuration(config, team_only=True)
    if settings.get("users") or settings.get("team", {}).get("type") != "git":
        parser.error("Нужен отдельный конфиг исполнителя с team.type=git и пустым users.")
    team = GitTeamConfig.from_json(settings["team"], config.parent, [])
    if args.install:
        run_pipeline(config, team_mode="process", validate_only=True)
        install_task(config, team, args.minutes)
        return 0
    return run_once(config, team)


if __name__ == "__main__":
    raise SystemExit(main())
