"""Полный цикл памяти пользователя через OpenRouter одной командой.

Вход: user-id, workspace, проекты Codex, модель и число диалогов.
Выход: анализ диалогов, автоматический replay-сценарий, консолидация,
ревью, обязательный тестовый прогон и опубликованная личная память.
Функции: run_command — последовательный запуск Python-команды;
main — разбор аргументов и выполнение полного цикла.
"""
import argparse
from pathlib import Path
import subprocess
import sys

from codex_replay_adapter import build_replay_scenario
from memory_pipeline import ROOT


def run_command(arguments: list[str]) -> None:
    """Принимает аргументы Python-скрипта; выполняет процесс или передаёт его ошибку."""
    subprocess.run([sys.executable, "-X", "utf8", *arguments], cwd=ROOT, check=True)


def main() -> None:
    """Читает параметры полного цикла, запускает все этапы и возвращает None."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, help="Идентификатор пользователя")
    parser.add_argument("--workspace", type=Path, required=True, help="Область памяти пользователя")
    parser.add_argument("--project", action="append", required=True, help="Проект Codex; можно повторить")
    parser.add_argument("--limit-per-project", type=int, default=1, help="Число последних диалогов проекта")
    parser.add_argument("--model", default="deepseek/deepseek-v4.1-flash-1017", help="Модель OpenRouter")
    parser.add_argument("--replay-session", help="ID диалога для прогона; без него выбирается лучший сценарий")
    args = parser.parse_args()
    workspace = (ROOT / args.workspace).resolve()
    if workspace == ROOT or not workspace.is_relative_to(ROOT):
        parser.error("workspace должен находиться внутри проекта.")
    analysis = [
        str(ROOT / "analyze_chats_openrouter.py"),
        "--user-id", args.user_id,
        "--workspace", str(workspace),
        "--source", "codex",
        "--limit-per-project", str(args.limit_per_project),
        "--model", args.model,
    ]
    for project in args.project:
        analysis.extend(("--project", project))
    print("\n1/3 Анализ диалогов", flush=True)
    run_command(analysis)
    reports = workspace / "raw/chat-reports-openrouter"
    scenario_directory = workspace / "raw/replay-scenario"
    print("\n2/3 Подготовка сценария проверочного прогона", flush=True)
    scenario = build_replay_scenario(reports, scenario_directory, args.replay_session)
    print(f"Сценарий: {scenario}", flush=True)
    print("\n3/3 Консолидация, ревью, прогон и публикация", flush=True)
    run_command([
        str(ROOT / "consolidate_reports_openrouter.py"),
        "--user-id", args.user_id,
        "--workspace", str(workspace),
        "--stage", "personal",
        "--model", args.model,
        "--evaluate-by-replay",
        "--force-replay",
        "--replay-scenario", str(scenario.relative_to(ROOT)),
    ])
    print("\nПолный цикл завершён.", flush=True)


if __name__ == "__main__":
    main()
