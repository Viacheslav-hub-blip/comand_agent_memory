"""Возобновление консолидации с сохранённого кандидата перед review.

Функции: copy_resume_inputs — перенос служебных материалов в новый запуск;
resume_personal_review — review, replay и публикация сохранённой личной памяти;
main — разбор аргументов командной строки и запуск восстановления.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

from memory_pipeline import ROOT
from openrouter_client import run_openrouter
from replay_evaluation import evaluate_by_replay, load_replay_scenario
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


def copy_resume_inputs(source: Path, destination: Path) -> None:
    """Принимает исходный и новый каталоги запусков, копирует служебные файлы.

    Выходом является заполненный новый каталог. Снимки памяти копируются через
    проверяемое чтение, остальные обязательные материалы — без изменения текста.
    """
    destination.mkdir(parents=True, exist_ok=False)
    for name in SUPPORT_FILES:
        source_path = source / name
        if not source_path.is_file():
            raise FileNotFoundError(f"Не найден материал возобновления: {source_path}")
        shutil.copy2(source_path, destination / name)
    before = read_memory(source / "before", "personal")
    candidate = read_memory(source / "before-review", "personal")
    write_snapshot(destination / "before", before)
    write_snapshot(destination / "before-review", candidate)
    write_snapshot(destination / "workspace/memory", candidate)
    (destination / "reviews").mkdir()


def resume_personal_review(source: Path, destination: Path, target: Path, model: str,
                           scenario_path: Path | None, agent_python: Path | None,
                           user_id: str) -> dict:
    """Возобновляет review личной памяти и возвращает итоговое состояние запуска.

    Входы: неудачный запуск, новый журнал, рабочая память, модель, необязательные
    replay-сценарий и Python агента, пользователь. Выход: словарь состояния.
    Без сценария публикация выполняется после принятого review; со сценарием также
    требуется выбор ``candidate`` в replay-сравнении.
    """
    source, destination, target = source.resolve(), destination.resolve(), target.resolve()
    if not source.is_relative_to(ROOT) or not destination.is_relative_to(ROOT):
        raise RuntimeError("Каталоги запуска должны находиться внутри проекта.")
    if not target.is_relative_to(ROOT) or target == ROOT:
        raise RuntimeError("Рабочая память должна находиться внутри проекта и не совпадать с корнем.")
    copy_resume_inputs(source, destination)
    if (scenario_path is None) != (agent_python is None):
        raise ValueError("replay-сценарий и Python агента указываются вместе.")
    scenario = load_replay_scenario(scenario_path.resolve()) if scenario_path else None
    before = read_memory(destination / "before", "personal")
    candidate = read_memory(destination / "before-review", "personal")
    status = {
        "stage": "personal",
        "target": str(target),
        "user_id": user_id,
        "resumed_from": str(source),
        "state": "running",
        "replay_status": "pending" if scenario else "not_requested",
    }
    proposal = MemoryProposal(target, "personal", destination, before, candidate)
    try:
        with memory_lock(target):
            if read_memory(target, "personal") != before:
                raise RuntimeError("Рабочая память изменилась после остановленного запуска.")
            write_stage_report(
                destination, "review", "Проверка и исправления", "running",
                "Review возобновлён из сохранённого снимка before-review.",
            )
            proposal, decision = review_memory(
                proposal, "", model, runner=run_openrouter,
            )
            write_stage_report(
                destination, "review", "Проверка и исправления", decision["decision"],
                decision["summary"], candidate, proposal.after,
                {"Решение": destination / "reviews/review-001.json"},
            )
            if decision["decision"] != "accepted":
                status["state"] = "rejected"
                raise RuntimeError(f"Память отклонена review: {decision['summary']}")
            status["reviews"] = decision["reviews"]
            if scenario and agent_python:
                status["replay_status"] = "running"
                write_stage_report(
                    destination, "replay", "Проверка решением задачи", "running",
                    "Выполняется обязательный replay с новой памятью.",
                )
                evaluation = evaluate_by_replay(
                    proposal, scenario, "", model, agent_python.resolve(), user_id,
                    backend="openrouter", runner=run_openrouter,
                )
                status.update(replay_status="completed", replay_choice=evaluation["choice"])
                write_stage_report(
                    destination, "replay", "Проверка решением задачи", "completed",
                    f"Выбор: {evaluation['choice']}\n\n{evaluation['summary']}",
                    links={"Сравнение": destination / "replay/decision.json",
                           "Результат": destination / "replay/result"},
                )
                if evaluation["choice"] != "candidate":
                    status["state"] = "rejected"
                    raise RuntimeError(f"Новая память не выбрана replay: {evaluation['summary']}")
            publish_memory(target, before, proposal.after, "personal", destination)
            status.update(
                state="published",
                snapshot_sha256={
                    name: hashlib.sha256(content).hexdigest()
                    for name, content in proposal.after.items()
                },
            )
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
            "Память принята после возобновления." if applied
            else "Память не принята. " + status.get("error", ""),
            before if applied else None, proposal.after if applied else None,
            {"Состояние запуска": destination / "status.json"},
        )
    return status


def main() -> None:
    """Читает аргументы CLI, возобновляет review/replay и печатает JSON состояния."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--destination-run", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--replay-scenario", type=Path)
    parser.add_argument("--agent-python", type=Path)
    parser.add_argument("--user-id", required=True)
    args = parser.parse_args()
    status = resume_personal_review(
        ROOT / args.source_run,
        ROOT / args.destination_run,
        ROOT / args.workspace,
        args.model,
        ROOT / args.replay_scenario if args.replay_scenario else None,
        args.agent_python,
        args.user_id,
    )
    print(json.dumps(status, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
