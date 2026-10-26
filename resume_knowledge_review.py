"""Возобновление review и публикации общей базы знаний.

Функции: copy_knowledge_resume_inputs — перенос снимков и служебных материалов;
resume_knowledge_review — повтор review и публикация общей базы;
main — разбор аргументов и вывод состояния запуска.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

from memory_pipeline import ROOT
from openrouter_client import run_openrouter
from reviewed_consolidation import (
    MemoryProposal,
    memory_lock,
    publish_memory,
    read_memory,
    review_memory,
    write_snapshot,
)
from run_reports import write_stage_report


SUPPORT_FILES = (
    "materials.md",
    "rules.md",
    "review-SKILL.md",
    "review-worked-examples.md",
)


def copy_knowledge_resume_inputs(source: Path, destination: Path) -> None:
    """Копирует снимки knowledge и служебные материалы в новый каталог запуска.

    Входы — неудачный и новый каталоги запусков. Выход — чистая копия кандидата
    ``before-review`` в ``workspace/memory`` и необходимые файлы для review.
    """
    destination.mkdir(parents=True, exist_ok=False)
    for name in SUPPORT_FILES:
        path = source / name
        if not path.is_file():
            raise FileNotFoundError(f"Не найден материал возобновления: {path}")
        shutil.copy2(path, destination / name)
    before = read_memory(source / "before", "knowledge")
    candidate = read_memory(source / "before-review", "knowledge")
    write_snapshot(destination / "before", before)
    write_snapshot(destination / "before-review", candidate)
    write_snapshot(destination / "workspace/memory", candidate)
    (destination / "reviews").mkdir()


def resume_knowledge_review(source: Path, destination: Path, target: Path, model: str) -> dict:
    """Возобновляет review общей базы знаний и возвращает итоговое состояние.

    Входы: остановленный запуск, каталог нового запуска, общий каталог знаний и
    модель. Выход: словарь состояния. Публикация выполняется только при решении
    review ``accepted``; личные рабочие области участников не изменяются.
    """
    source, destination, target = source.resolve(), destination.resolve(), target.resolve()
    if not source.is_relative_to(ROOT) or not destination.is_relative_to(ROOT):
        raise RuntimeError("Каталоги запусков должны находиться внутри проекта.")
    if not target.is_relative_to(ROOT) or target == ROOT:
        raise RuntimeError("Каталог знаний должен находиться внутри проекта.")
    copy_knowledge_resume_inputs(source, destination)
    before = read_memory(destination / "before", "knowledge")
    candidate = read_memory(destination / "before-review", "knowledge")
    proposal = MemoryProposal(target, "knowledge", destination, before, candidate)
    status = {
        "stage": "knowledge",
        "target": str(target),
        "resumed_from": str(source),
        "state": "running",
        "replay_status": "not_requested",
    }
    try:
        with memory_lock(target):
            if read_memory(target, "knowledge") != before:
                raise RuntimeError("Общая база знаний изменилась после остановленного запуска.")
            write_stage_report(
                destination, "review", "Проверка и исправления", "running",
                "Review общей базы возобновлён из сохранённого снимка before-review.",
            )
            proposal, decision = review_memory(proposal, "", model, runner=run_openrouter)
            write_stage_report(
                destination, "review", "Проверка и исправления", decision["decision"],
                decision["summary"], candidate, proposal.after,
                {"Решение": destination / "reviews/review-001.json"},
            )
            if decision["decision"] != "accepted":
                status["state"] = "rejected"
                raise RuntimeError(f"Общая база отклонена review: {decision['summary']}")
            status["reviews"] = decision["reviews"]
            publish_memory(target, before, proposal.after, "knowledge", destination)
            status["state"] = "published"
    except BaseException as error:
        status.setdefault("error", str(error))
        if status["state"] == "running":
            status["state"] = "failed"
        raise
    finally:
        (destination / "status.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        applied = status["state"] == "published"
        write_stage_report(
            destination, "publication", "Принятие памяти", status["state"],
            "Общая база знаний принята после возобновления." if applied
            else "Общая база знаний не принята. " + status.get("error", ""),
            before if applied else None, proposal.after if applied else None,
            {"Состояние запуска": destination / "status.json"},
        )
    return status


def main() -> None:
    """Читает аргументы CLI, запускает возобновление и печатает JSON состояния."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--destination-run", type=Path, required=True)
    parser.add_argument("--knowledge-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    result = resume_knowledge_review(
        ROOT / args.source_run,
        ROOT / args.destination_run,
        ROOT / args.knowledge_dir,
        args.model,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
