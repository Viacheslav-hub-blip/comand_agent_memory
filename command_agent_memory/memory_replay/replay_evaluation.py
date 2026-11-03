"""Один проверочный прогон с предложенной памятью и оценка его результата.

Функции: is_replay_decision проверяет схему; parse_replay_decision читает решение;
copy_directory копирует входы; evaluate_by_replay выполняет диалог в копии
и возвращает выбор candidate/current/inconclusive без публикации памяти.
"""
import json
from pathlib import Path
import shutil
from time import perf_counter
from typing import Callable

from command_agent_memory.model_execution.codex_cli import run_codex
from command_agent_memory.skill_resources import get_skill_path
from command_agent_memory.memory_update.memory_proposal import MemoryProposal
from command_agent_memory.memory_storage.memory_layout import PERSONAL_MEMORY_ENTRIES
from command_agent_memory.memory_storage.memory_snapshots import read_memory, write_snapshot
from command_agent_memory.memory_replay.replay_scenario import ReplayScenario


def is_replay_decision(value: object) -> bool:
    """Принимает произвольное значение и возвращает признак строгого replay-решения.

    Допускаются ровно поля ``choice`` и ``summary``. Выбор должен быть одним из
    ``candidate``, ``current`` или ``inconclusive``, а пояснение — непустой строкой.
    """
    return (
        isinstance(value, dict)
        and set(value) == {"choice", "summary"}
        and value.get("choice") in ("candidate", "current", "inconclusive")
        and isinstance(value.get("summary"), str)
        and bool(value["summary"].strip())
    )


def parse_replay_decision(reply: str) -> dict:
    """Принимает текст оценщика и возвращает последнее строгое replay-решение.

    Парсер допускает пояснение и Markdown вокруг JSON, но принимает только объект
    точной схемы. При отсутствии подходящего объекта вызывает ``ValueError``.
    """
    decoder = json.JSONDecoder()
    decisions = []
    for index, character in enumerate(reply):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(reply, index)
        except json.JSONDecodeError:
            continue
        if is_replay_decision(value):
            decisions.append(value)
    if not decisions:
        raise ValueError("Ответ оценщика не содержит replay-решения строгой JSON-схемы.")
    return decisions[-1]


def copy_directory(source: Path, destination: Path) -> None:
    """Принимает исходный и новый каталоги; копирует файлы, возвращает None.

    Символические ссылки и NTFS reparse points не копируются. Вложенная
    в источник цель не допускается. Оригиналы не изменяются.
    """
    if destination.resolve().is_relative_to(source.resolve()):
        raise ValueError("Каталог копии не должен находиться внутри источника.")
    paths = []
    pending = [source]
    while pending:
        path = pending.pop()
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Ссылка в сценарии не допускается: {path}")
        paths.append(path)
        if path.is_dir():
            pending.extend(path.iterdir())
    destination.mkdir(parents=True, exist_ok=False)
    for path in paths[1:]:
        target = destination / path.relative_to(source)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def evaluate_by_replay(proposal: MemoryProposal, scenario: ReplayScenario,
                       executable: str, model: str, user_id: str,
                       runner: Callable | None = None, runtime=None) -> dict:
    """Принимает предложение, сценарий, исполнителя и пользователя; возвращает выбор LLM.

    Один раз запускает решающего агента в копии. Прежние ответы передаются только
    оценщику. Любой сбой вызывает исключение без переноса памяти. Выбор current
    или inconclusive также не является разрешением переноса.
    runtime выбирает независимые модели решающего агента, тестировщика и оценщика;
    proposal.layout задаёт защищённые пути внешней памяти.
    """
    runner = runner or run_codex
    if scenario.inputs is None or scenario.reference is None or not scenario.request.strip():
        raise ValueError("Для выполнения прогона нужен запрос, inputs и reference.")
    if not scenario.messages:
        raise ValueError("Для диалогового прогона нужны отдельные messages либо dialogue_source.")
    directory = proposal.run / "replay"
    directory.mkdir()
    workspace = directory / "workspace"
    write_snapshot(workspace, proposal.after)
    if proposal.layout is not None:
        if any(Path(scenario.inputs.name).is_relative_to(Path(name))
               or Path(name).is_relative_to(Path(scenario.inputs.name)) for name in proposal.layout.entries):
            raise ValueError("Входные файлы прогона пересекаются с настроенной памятью.")
    copy_directory(scenario.inputs, workspace / scenario.inputs.name)
    if (proposal.target / "skills").is_dir():
        copy_directory(proposal.target / "skills", workspace / "skills")
    copy_directory(scenario.reference, directory / "reference")
    (directory / "request.md").write_text(scenario.request, encoding="utf-8")
    (directory / "reference-metrics.json").write_text(
        json.dumps(scenario.reference_metrics, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    completed = False
    started = perf_counter()
    print(f"Один проверочный прогон: {directory}", flush=True)
    try:
        from command_agent_memory.memory_replay.dialogue_runner import run_dialogue

        options = ({"runtime": runtime, "memory_layout": proposal.layout} if runtime is not None else {})
        run_dialogue(workspace, scenario, directory / "result", model,
                     user_id=user_id, memory_enabled=True, **options)
        completed = True
    finally:
        (directory / "execution.json").write_text(json.dumps({
            "backend": "dialogue", "model": model,
            "duration_seconds": perf_counter() - started,
            "return_code": 0 if completed else None,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    metrics = json.loads((directory / "result/metrics.json").read_text(encoding="utf-8"))
    if metrics.get("state") != "completed" or not (directory / "result/answer.md").is_file():
        raise RuntimeError("Прогон не сохранил завершённый результат.")
    if read_memory(workspace, "personal", proposal.layout) != proposal.after:
        raise RuntimeError("Агент изменил память во время прогона; перенос отменён.")
    files = directory / "result/files"
    files.mkdir()
    for path in workspace.iterdir():
        if proposal.layout is not None:
            relative = path.relative_to(workspace)
            if any(relative.is_relative_to(Path(name)) or Path(name).is_relative_to(relative)
                   for name in proposal.layout.entries):
                continue
        if path.name in (*PERSONAL_MEMORY_ENTRIES, "skills"):
            continue
        if path.is_dir():
            copy_directory(path, files / path.name)
        else:
            if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError(f"Ссылка в результате не допускается: {path}")
            shutil.copy2(path, files / path.name)
    skill = get_skill_path("replay-evaluation")
    (directory / "evaluation").mkdir()
    prompt = (
        skill.read_text(encoding="utf-8")
        + f"\n\nЗапрос: {(directory / 'request.md').as_posix()}. "
        + f"Исходные данные: {scenario.inputs.as_posix()}. "
        + f"Прежний результат: {(directory / 'reference').as_posix()}. "
        + f"Прежние измерения: {(directory / 'reference-metrics.json').as_posix()}. "
        + f"Новый результат, файлы, трасса и измерения: {(directory / 'result').as_posix()}. "
        + "Сравни результаты. Файлы не изменяй. Верни JSON по skill."
    )
    judge_runner = runtime.runner("evaluation") if runtime is not None else runner
    reply = judge_runner(executable, model, prompt, directory / "evaluation", directory / "evaluation/judge")
    decision = parse_replay_decision(reply)
    (directory / "decision.json").write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    return decision
