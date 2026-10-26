"""Завершение публикации по уже сохранённому ответу replay-оценщика.

Функции: finalize_replay_run — проверка артефактов, разбор решения и публикация;
main — чтение аргументов командной строки и печать итогового состояния.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from memory_pipeline import ROOT
from replay_evaluation import parse_replay_decision
from reviewed_consolidation import memory_lock, publish_memory, read_memory
from run_reports import write_stage_report


def finalize_replay_run(run: Path, target: Path) -> dict:
    """Принимает каталог replay-запуска и память, возвращает итог публикации.

    Функция повторно проверяет снимки памяти, завершённость результата и сохранённый
    ответ оценщика. Память переносится только для строгого решения ``candidate``.
    """
    run, target = run.resolve(), target.resolve()
    if not run.is_relative_to(ROOT) or not target.is_relative_to(ROOT) or target == ROOT:
        raise RuntimeError("Запуск и память должны находиться внутри проекта.")
    before = read_memory(run / "before", "personal")
    after = read_memory(run / "after", "personal")
    replay = run / "replay"
    metrics = json.loads((replay / "result/metrics.json").read_text(encoding="utf-8"))
    if metrics.get("state") != "completed" or not (replay / "result/answer.md").is_file():
        raise RuntimeError("Сохранённый replay не содержит завершённого результата.")
    if read_memory(replay / "workspace", "personal") != after:
        raise RuntimeError("Память в replay-области отличается от принятого review-снимка.")
    reply = (replay / "evaluation/judge.last.txt").read_text(encoding="utf-8")
    decision = parse_replay_decision(reply)
    (replay / "decision.json").write_text(
        json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    status = {
        "stage": "personal",
        "target": str(target),
        "state": "running",
        "replay_status": "completed",
        "replay_choice": decision["choice"],
        "resumed_from_saved_judge": True,
    }
    try:
        if decision["choice"] != "candidate":
            status["state"] = "rejected"
            raise RuntimeError(f"Новая память не выбрана replay: {decision['summary']}")
        with memory_lock(target):
            if read_memory(target, "personal") != before:
                raise RuntimeError("Рабочая память изменилась после review.")
            publish_memory(target, before, after, "personal", run)
        status["state"] = "published"
        write_stage_report(
            run, "replay", "Проверка решением задачи", "completed",
            f"Выбор: {decision['choice']}\n\n{decision['summary']}",
            links={"Сравнение": replay / "decision.json", "Результат": replay / "result"},
        )
    except BaseException as error:
        status.setdefault("error", str(error))
        if status["state"] == "running":
            status["state"] = "failed"
        raise
    finally:
        (run / "status.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        applied = status["state"] == "published"
        write_stage_report(
            run, "publication", "Принятие памяти", status["state"],
            "Память принята после разбора сохранённого решения оценщика."
            if applied else "Память не принята. " + status.get("error", ""),
            before if applied else None, after if applied else None,
            {"Состояние запуска": run / "status.json"},
        )
    return status


def main() -> None:
    """Читает пути запуска и памяти, завершает публикацию и печатает JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    status = finalize_replay_run(ROOT / args.run, ROOT / args.workspace)
    print(json.dumps(status, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
