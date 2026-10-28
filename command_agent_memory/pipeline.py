"""Сквозной цикл схемы для одного или нескольких пользователей.

Вход: CLI-параметры либо JSON со списком пользователей и существующими источниками.
Выход: новые отчёты, принятые личные снимки, общая память и team_knowledge/.
Функции: load_users — проверка областей пользователей; find_accepted_run — поиск
принятой версии тех же отчётов; team_is_current — проверка повторного запуска;
main — выполнение анализа, личных циклов, командного объединения и доставки.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from memory_pipeline import ROOT
from command_agent_memory.analysis.pipeline import main as analyze
from command_agent_memory.analysis.reports import report_fingerprint
from command_agent_memory.memory.cli import main as consolidate
from command_agent_memory.memory.personal import read_memory
from command_agent_memory.memory.team import read_accepted_knowledge, sync_team_memory
from command_agent_memory.runs import create_run_directory
from codex_replay_adapter import ReplayDataUnavailable, build_replay_scenario


def load_users(entries: list[dict]) -> list[dict]:
    """Принимает описания пользователей; возвращает проверенные независимые задания.

    Пути разрешаются от корня проекта. Повторы ID, пересекающиеся области и
    неизвестные поля вызывают ValueError до создания файлов и вызовов модели.
    """
    if not isinstance(entries, list) or not entries:
        raise ValueError("Нужен непустой список пользователей.")
    result, identities = [], set()
    allowed = {"user_id", "workspace", "source", "projects", "replay_scenario"}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - allowed:
            raise ValueError("Неизвестные поля в описании пользователя.")
        identity = entry.get("user_id")
        if (not isinstance(identity, str) or not identity
                or not all(character.isalnum() or character in "_-" for character in identity)):
            raise ValueError("user_id должен содержать буквы, цифры, _ или -")
        if identity in identities:
            raise ValueError(f"Повтор пользователя: {identity}")
        if not isinstance(entry.get("workspace"), str) or not entry["workspace"]:
            raise ValueError("Для пользователя нужен workspace.")
        workspace = (ROOT / entry["workspace"]).resolve()
        if workspace == ROOT or not workspace.is_relative_to(ROOT):
            raise ValueError("Рабочая область должна находиться внутри проекта.")
        if any(workspace.is_relative_to(item["workspace"]) or item["workspace"].is_relative_to(workspace)
               for item in result):
            raise ValueError("Области пользователей не должны пересекаться.")
        source, projects = entry.get("source", "sqlite"), entry.get("projects", [])
        if source not in ("sqlite", "codex"):
            raise ValueError("Поддерживаются существующие источники sqlite и codex.")
        if not isinstance(projects, list) or any(not isinstance(value, str) or not value for value in projects):
            raise ValueError("projects должен содержать список имён проектов.")
        if source == "codex" and not projects:
            raise ValueError("Для Codex нужен список projects.")
        scenario = entry.get("replay_scenario")
        if scenario is not None and (not isinstance(scenario, str) or not scenario):
            raise ValueError("replay_scenario должен содержать путь либо null.")
        identities.add(identity)
        result.append({"user_id": identity, "workspace": workspace, "source": source,
                       "projects": projects, "replay_scenario": scenario})
    return result


def find_accepted_run(workspace: Path, fingerprint: str) -> Path | None:
    """Принимает область и версию отчётов; возвращает уже принятый неизменный личный запуск.

    Сравнивает технические версии входов и опубликованных файлов. Это повторное
    использование результата, а не реестр подтверждений знания.
    """
    for path in sorted((ROOT / "data/consolidation-runs").glob("*/personal/status.json"), reverse=True):
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if (metadata.get("target") != str(workspace) or metadata.get("report_fingerprint") != fingerprint
                or metadata.get("state") not in ("published", "unchanged")):
            continue
        current = {name: hashlib.sha256(content).hexdigest()
                   for name, content in read_memory(workspace, "personal").items()}
        if current == metadata.get("snapshot_sha256"):
            read_accepted_knowledge(path.parent)
            return path.parent
    return None


def team_is_current(target: Path, accepted_runs: list[Path]) -> bool:
    """Принимает каталог команды и личные версии; возвращает наличие их принятого объединения."""
    inputs = {str(path) for path in accepted_runs}
    current = {name: hashlib.sha256(content).hexdigest()
               for name, content in read_memory(target, "knowledge").items()}
    for path in sorted((ROOT / "data/consolidation-runs").glob("*/knowledge/status.json"), reverse=True):
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if (metadata.get("stage") == "team" and metadata.get("target") == str(target)
                and metadata.get("state") in ("published", "unchanged")
                and set(metadata.get("accepted_runs", [])) == inputs
                and metadata.get("snapshot_sha256") == current):
            return True
    return False


def main(argv: list[str] | None = None) -> None:
    """Принимает CLI-аргументы; выполняет независимые личные циклы и общую консолидацию.

    validate-only проверяет задания без моделей и записи. Без запрошенной новой
    проверки запуск с неизменными входами использует уже принятые результаты.
    Реестр, pending и conflicts остаются отложенными.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(value == "--config" or value.startswith("--config=") for value in arguments):
        from command_agent_memory.connections import run_connections

        parser = argparse.ArgumentParser(description="Подключение истории и памяти рабочего агента")
        parser.add_argument("--config", type=Path, required=True)
        parser.add_argument("--validate-only", action="store_true")
        parser.add_argument("--evaluate-by-replay", action="store_true")
        args = parser.parse_args(arguments)
        run_connections(args.config, validate_only=args.validate_only,
                        evaluate_by_replay=args.evaluate_by_replay)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="JSON подключения внешнего агента; отдельный режим")
    parser.add_argument("--users", type=Path, help="JSON со списком заданий пользователей")
    parser.add_argument("--user-id")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--source", choices=("sqlite", "codex"), default="sqlite")
    parser.add_argument("--project", action="append", default=[])
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--limit-per-project", type=int)
    parser.add_argument("--backend", choices=("codex", "openrouter"), default="openrouter")
    parser.add_argument("--model", default="deepseek/deepseek-v4.1-flash-1017")
    parser.add_argument("--review-model")
    parser.add_argument("--knowledge-dir", type=Path, default=Path("knowledge"))
    parser.add_argument("--stage", choices=("personal", "both"), default="both")
    parser.add_argument("--evaluate-by-replay", action="store_true")
    parser.add_argument("--replay-scenario", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    if args.users:
        if args.user_id or args.workspace or args.project or args.replay_scenario:
            parser.error("--users задаёт пользователей отдельно от одиночных параметров.")
        entries = json.loads((ROOT / args.users).read_text(encoding="utf-8-sig"))
    else:
        entries = [{"user_id": args.user_id, "workspace": str(args.workspace) if args.workspace else None,
                    "source": args.source, "projects": args.project,
                    "replay_scenario": str(args.replay_scenario) if args.replay_scenario else None}]
    users = load_users(entries)
    if not args.evaluate_by_replay and any(user["replay_scenario"] for user in users):
        parser.error("Сценарий используется с --evaluate-by-replay.")
    knowledge = (ROOT / args.knowledge_dir).resolve()
    if knowledge == ROOT or not knowledge.is_relative_to(ROOT):
        parser.error("Командная память должна находиться внутри проекта.")
    for user in users:
        if knowledge.is_relative_to(user["workspace"]) or user["workspace"].is_relative_to(knowledge):
            parser.error("Командная память и области пользователей не должны пересекаться.")
        arguments = ["--user-id", user["user_id"], "--workspace", str(user["workspace"]),
                     "--source", user["source"], "--model", args.model, "--backend", args.backend]
        for project in user["projects"]:
            arguments.extend(("--project", project))
        if args.codex_home:
            arguments.extend(("--codex-home", str(args.codex_home)))
        if args.limit_per_project is not None:
            arguments.extend(("--limit-per-project", str(args.limit_per_project)))
        user["analysis_arguments"] = arguments
        analyze([*arguments, "--validate-only"])
    if args.validate_only:
        print("Задания проверены; модели не вызывались, файлы не создавались.")
        return
    accepted_runs = []
    for user in users:
        reports = analyze(user["analysis_arguments"])
        if not reports.is_dir() or not any(reports.glob("session-*")):
            print(f"Для {user['user_id']} пока нет сессий и аналитических отчётов.")
            continue
        accepted = (None if args.evaluate_by_replay else
                    find_accepted_run(user["workspace"], report_fingerprint(reports)))
        if accepted is None:
            arguments = ["--user-id", user["user_id"], "--workspace", str(user["workspace"]),
                         "--reports-dir", str(reports), "--backend", args.backend, "--model", args.model,
                         "--stage", "personal"]
            if args.review_model:
                arguments.extend(("--review-model", args.review_model))
            if args.evaluate_by_replay:
                arguments.append("--evaluate-by-replay")
                scenario = user["replay_scenario"]
                if scenario is None and user["source"] == "codex":
                    scenario_run = create_run_directory(user["workspace"] / "raw/replay-scenarios")
                    try:
                        scenario = build_replay_scenario(reports, scenario_run / "scenario")
                    except ReplayDataUnavailable as error:
                        print(f"Данные для прогона недоступны: {error}")
                if scenario:
                    arguments.extend(("--replay-scenario", str(scenario)))
            accepted = consolidate(arguments) / "personal"
        else:
            print(f"Личная память {user['user_id']} уже принята для этих отчётов: {accepted}")
        accepted_runs.append(accepted)
    if args.stage == "both" and accepted_runs:
        if not team_is_current(knowledge, accepted_runs):
            arguments = ["--stage", "knowledge", "--knowledge-dir", str(knowledge),
                         "--backend", args.backend, "--model", args.model]
            for accepted in accepted_runs:
                arguments.extend(("--accepted-run", str(accepted)))
            consolidate(arguments)
        else:
            sync_run = create_run_directory(ROOT / "data/team-sync-runs")
            for user in users:
                sync_team_memory(knowledge, user["workspace"], sync_run / user["user_id"])
            print("Командная версия уже принята; подключённые копии проверены.")
