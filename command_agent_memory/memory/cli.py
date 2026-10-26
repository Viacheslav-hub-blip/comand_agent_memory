"""Команды личного цикла и объединения принятых знаний команды.

Вход: CLI-параметры области пользователя, отчётов, личных запусков и моделей.
Выход: личная и/или командная память, подключённые снимки и каталоги журналов.
Функция main разбирает аргументы, проверяет входы и запускает этапы схемы.
"""
import argparse
from pathlib import Path


def main(argv: list[str] | None = None) -> Path | None:
    """Принимает аргументы CLI; выполняет выбранные этапы и возвращает каталог запуска.

    validate-only не вызывает моделей и не записывает файлы. Командный этап
    принимает только --accepted-run либо результат текущего личного этапа.
    """
    from memory_pipeline import ROOT, read_reports
    from command_agent_memory.adapters import load_model_adapter
    from command_agent_memory.runs import create_run_directory
    from reviewed_consolidation import KNOWLEDGE_SKILL, consolidate_with_review
    from replay_evaluation import load_replay_scenario
    from command_agent_memory.memory.team import (
        consolidate_team_memory, read_accepted_knowledge, sync_team_memory,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--reports-dir", type=Path)
    parser.add_argument("--knowledge-dir", type=Path, default=Path("knowledge"))
    parser.add_argument("--stage", choices=("personal", "knowledge", "both"), default="both")
    parser.add_argument("--accepted-run", type=Path, action="append", default=[],
                        help="Принятый личный запуск; можно передать несколько пользователей")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--backend", choices=("codex", "openrouter"), default="codex")
    parser.add_argument("--review-model")
    parser.add_argument("--codex-path", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--evaluate-by-replay", action="store_true",
                        help="При достаточных данных выполнить диалог с новой памятью")
    parser.add_argument("--replay-scenario", type=Path)
    args = parser.parse_args(argv)
    personal = args.stage in ("personal", "both")
    workspace = (ROOT / args.workspace).resolve() if args.workspace else None
    knowledge = (ROOT / args.knowledge_dir).resolve()
    if personal and (workspace is None or not args.user_id):
        parser.error("Для личного этапа нужны --user-id и --workspace.")
    if args.user_id and not all(character.isalnum() or character in "_-" for character in args.user_id):
        parser.error("user-id должен содержать буквы, цифры, _ или -")
    if not personal and (args.evaluate_by_replay or args.replay_scenario):
        parser.error("Диалоговая проверка применяется только к личной памяти.")
    if args.replay_scenario and not args.evaluate_by_replay:
        parser.error("--replay-scenario используется с --evaluate-by-replay.")
    paths = [knowledge] + ([workspace] if workspace else [])
    for path in paths:
        if path == ROOT or not path.is_relative_to(ROOT):
            parser.error(f"Укажи отдельный каталог внутри проекта: {path}")
    if workspace and (workspace.is_relative_to(knowledge) or knowledge.is_relative_to(workspace)):
        parser.error("Личная область и каталог знаний не должны пересекаться")
    accepted_runs = [(ROOT / path).resolve() for path in args.accepted_run]
    if args.stage == "knowledge" and not accepted_runs:
        parser.error("Командному этапу нужны --accepted-run с принятыми личными знаниями.")
    if args.stage == "personal" and accepted_runs:
        parser.error("--accepted-run относится только к командному этапу.")
    for path in accepted_runs:
        owner = Path(read_accepted_knowledge(path)[0]["target"])
        if knowledge.is_relative_to(owner) or owner.is_relative_to(knowledge):
            parser.error("Командная память не должна пересекаться с областью владельца снимка.")
    context_reports = procedure_reports = ""
    reports = None
    scenario = None
    if personal:
        if not workspace.is_dir():
            parser.error(f"Область пользователя не найдена: {workspace}")
        folder = "chat-reports-openrouter" if args.backend == "openrouter" else "chat-reports-codex"
        reports = (ROOT / args.reports_dir).resolve() if args.reports_dir else workspace / "raw" / folder
        if reports == ROOT or not reports.is_relative_to(ROOT):
            parser.error("Каталог отчётов должен находиться внутри проекта.")
        if reports.is_relative_to(knowledge) or knowledge.is_relative_to(reports):
            parser.error("Каталог знаний и входные отчёты не должны пересекаться")
        context_reports, procedure_reports, count = read_reports(reports, user_id=args.user_id)
        if not count:
            parser.error(f"Нет отчётов в {reports}. Сначала запусти анализ.")
        if args.replay_scenario:
            scenario = load_replay_scenario(ROOT / args.replay_scenario, allow_incomplete=True)
    print("Этапы: " + ("personal, knowledge" if args.stage == "both" else args.stage))
    if args.evaluate_by_replay:
        print("Проверяем наличие данных; при нехватке возможен пропуск с принятием по ревью.")
    print(f"Каталог знаний: {knowledge}; реестр подтверждений не реализован.")
    if args.validate_only:
        return None
    adapter = load_model_adapter(args.backend, args.codex_path)
    run = create_run_directory(ROOT / "data/consolidation-runs")
    if personal:
        consolidate_with_review(
            workspace, "personal", context_reports,
            ROOT / "agents/consolidation/skills/consolidation/SKILL.md",
            adapter.executable, args.model, run / "personal",
            review_model=args.review_model, knowledge_materials=procedure_reports,
            replay_scenario=scenario, user_id=args.user_id,
            replay_requested=args.evaluate_by_replay, replay_context=reports,
            runner=adapter.agent_runner,
        )
        accepted_runs.append(run / "personal")
    if args.stage in ("knowledge", "both"):
        consolidate_team_memory(knowledge, accepted_runs, run / "knowledge",
                                adapter.executable, args.model, adapter.agent_runner)
        workspaces = {Path(read_accepted_knowledge(path)[0]["target"]) for path in accepted_runs}
        for index, destination in enumerate(sorted(workspaces)):
            sync_team_memory(knowledge, destination, run / "team-sync" / str(index))
    print(f"Готово. Журналы: {run}")
    return run
