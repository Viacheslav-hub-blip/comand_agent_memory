"""Запускает диалоговую проверку без памяти и с выбранным снимком памяти.

Функции: prepare_workspace — создание чистой области; token_totals — сумма
токенов; write_comparison — таблица сравнения; main — обработка CLI и запуск.
"""

import argparse
import json
from pathlib import Path

from command_agent_memory.runs import create_run_directory
from memory_pipeline import ROOT
from replay_evaluation import copy_directory, load_replay_scenario
from reviewed_consolidation import read_memory, write_snapshot


def prepare_workspace(workspace: Path, inputs: Path, memory: Path | None = None,
                      team_knowledge: Path | None = None) -> None:
    """Принимает новые пути и снимки; копирует входы и выбранную память в область.

    Сообщения прежних чатов, результаты и журналы пользователя остаются в своих
    каталогах. Командные знания размещаются отдельно в team_knowledge/.
    """
    files = read_memory(memory, "personal") if memory else {}
    write_snapshot(workspace, files)
    copy_directory(inputs, workspace / inputs.name)
    if memory and (memory / "skills").is_dir():
        copy_directory(memory / "skills", workspace / "skills")
    if memory and team_knowledge:
        team_root = team_knowledge if (team_knowledge / "INDEX.md").is_file() else team_knowledge / "knowledge"
        if not (team_root / "INDEX.md").is_file():
            raise ValueError("В командной памяти нужен INDEX.md либо knowledge/INDEX.md.")
        copy_directory(team_root, workspace / "team_knowledge")


def token_totals(metrics: dict) -> tuple[int, int, int]:
    """Принимает показатели роли; возвращает вход, сохранённый вход и выход."""
    usage = metrics.get("usage") or {}
    values = [usage] if "input_tokens" in usage else usage.values()
    inputs = cached = outputs = 0
    for value in values:
        inputs += int(value.get("input_tokens") or 0)
        cached += int(value.get("cached_input_tokens") or (value.get("input_token_details") or {}).get("cache_read") or 0)
        outputs += int(value.get("output_tokens") or 0)
    return inputs, cached, outputs


def write_comparison(run: Path, results: dict) -> None:
    """Принимает каталог и показатели вариантов; сохраняет JSON и читаемую таблицу.

    Показатели тестируемого агента и пользователя-тестировщика выводятся в своих
    строках. Исход опыта и число пользовательских реплик включены в сравнение.
    """
    (run / "comparison.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Диалоговая проверка памяти", "",
             "В каждом варианте новый агент, отдельный контекст и одинаковая первая реплика.", "",
             "| Вариант | Роль | Время, с | Модель | Инструменты | Вход | Сохранённый вход | Выход | Полнота учёта |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for variant, metrics in results.items():
        for role in ("tested_agent", "tester"):
            item = metrics.get(role) or {}
            inputs, cached, outputs = token_totals(item)
            lines.append(f"| {variant} | {role} | {item.get('duration_seconds', 0):.2f} | "
                         f"{item.get('model_calls', 0)} | {item.get('tool_calls', 0)} | {inputs} | "
                         f"{cached} | {outputs} | {item.get('usage_complete', False)} |")
        lines.extend(["", f"Вариант {variant}: {metrics['state']}; реплик пользователя: {metrics['user_turns']}; "
                      f"полное время: {metrics['duration_seconds']:.2f} с.", ""])
    (run / "comparison.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    """Читает CLI; проверяет сценарий либо выполняет выбранные диалоги и сравнение."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--tester-model", help="Модель пользователя-тестировщика; по умолчанию совпадает с --model")
    parser.add_argument("--variant", choices=("no-memory", "full", "both"), default="both")
    parser.add_argument("--memory-source", type=Path)
    parser.add_argument("--team-knowledge", type=Path)
    parser.add_argument("--output", type=Path, help="Новый каталог запуска")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--max-user-turns", type=int)
    parser.add_argument("--recursion-limit", type=int, default=1000)
    parser.add_argument("--artifact-text-limit", type=int, default=120_000)
    args = parser.parse_args()
    scenario = load_replay_scenario(ROOT / args.scenario)
    if not scenario.messages:
        parser.error("Для диалога задайте messages либо dialogue_source в сценарии.")
    if args.variant in ("full", "both") and (args.memory_source is None or not args.memory_source.is_dir()):
        parser.error("Для полной памяти нужен существующий --memory-source.")
    if args.team_knowledge and (args.memory_source is None or not args.team_knowledge.is_dir()):
        parser.error("Командная память требует --memory-source и существующий каталог знаний.")
    if args.recursion_limit < 1 or args.artifact_text_limit < 1 or (args.max_user_turns is not None and args.max_user_turns < 1):
        parser.error("Лимиты должны быть положительными.")
    if args.validate_only:
        print(json.dumps({"first_user_message": scenario.messages[0], "source_messages": len(scenario.messages),
                          "variant": args.variant, "inputs": str(scenario.inputs),
                          "reference_for_tester": str(scenario.reference)}, ensure_ascii=False, indent=2))
        return
    from command_agent_memory.evaluation.dialogue import run_dialogue
    from openrouter_client import validate_openrouter

    validate_openrouter()
    if args.output:
        run = args.output.resolve()
        run.mkdir(parents=True, exist_ok=False)
    else:
        run = create_run_directory(ROOT / "data/dialogue-runs")
    (run / "scenario.json").write_text(json.dumps({
        "source": str(args.scenario.resolve()), "messages": list(scenario.messages),
        "goal": scenario.goal, "memory_source": str(args.memory_source.resolve()) if args.memory_source else None,
        "team_knowledge": str(args.team_knowledge.resolve()) if args.team_knowledge else None,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    results = {}
    variants = ("no-memory", "full") if args.variant == "both" else (args.variant,)
    print(f"Результаты: {run}", flush=True)
    for variant in variants:
        workspace = run / variant / "workspace"
        prepare_workspace(workspace, scenario.inputs,
                          args.memory_source if variant == "full" else None,
                          args.team_knowledge if variant == "full" else None)
        result = run / variant / "result"
        try:
            results[variant] = run_dialogue(
                workspace, scenario, result, args.model, memory_enabled=variant == "full",
                tester_model=args.tester_model, max_user_turns=args.max_user_turns,
                recursion_limit=args.recursion_limit, artifact_text_limit=args.artifact_text_limit,
            )
        except Exception:
            if (result / "metrics.json").is_file():
                results[variant] = json.loads((result / "metrics.json").read_text(encoding="utf-8"))
            raise
        finally:
            write_comparison(run, results)
        print(f"{variant}: {results[variant]['state']}; реплик: {results[variant]['user_turns']}", flush=True)


if __name__ == "__main__":
    main()
