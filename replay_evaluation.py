"""Один прогон задачи с предложенной памятью и сравнение результатов через LLM.

Вход: предложение памяти, сценарий, Python решающего агента и Codex оценщика.
Выход: копия окружения, результат задачи, измерения, журналы и решение оценщика.
Класс ReplayScenario хранит запрос, пути входов и прежнего результата, измерения.
Функции: load_replay_scenario — чтение сценария; assess_replay — оценка возможности и затрат;
copy_directory — копирование;
evaluate_by_replay — один запуск агента и оценка без переноса действующей памяти.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
from time import perf_counter
from typing import Callable

from codex_client import run_codex
from memory_pipeline import ROOT
from reviewed_consolidation import MemoryProposal, PERSONAL_MEMORY_ENTRIES, read_memory, write_snapshot


@dataclass(frozen=True)
class ReplayScenario:
    """Принимает запрос, пути и измерения; хранит полный или неполный сценарий.

    history указывает на сохранённую историю, resource_notes поясняет затраты.
    Отсутствующие inputs/reference допустимы для оценки, но не для выполнения.
    """

    request: str
    inputs: Path | None
    reference: Path | None
    reference_metrics: dict | None = None
    history: Path | None = None
    resource_notes: str = ""


def load_replay_scenario(path: Path, allow_incomplete: bool = False) -> ReplayScenario:
    """Принимает путь JSON; возвращает сценарий с абсолютными путями либо вызывает ValueError.

    Пути inputs и reference разрешаются относительно JSON внутри проекта.
    В режиме allow_incomplete отсутствие запроса, входов или ответа передаётся LLM
    для оценки. Типы полей и границы путей проверяются в обоих режимах.
    """
    path = path.resolve()
    if not path.is_file() or not path.is_relative_to(ROOT):
        raise ValueError("Сценарий должен быть JSON-файлом внутри проекта.")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("request", ""), str):
        raise ValueError("Сценарий должен быть объектом с текстовым request.")
    if not allow_incomplete and not data.get("request", "").strip():
        raise ValueError("В сценарии нужен непустой request.")
    directories = []
    for name in ("inputs", "reference"):
        if allow_incomplete and data.get(name) in (None, ""):
            directories.append(None)
            continue
        if not isinstance(data.get(name), str) or not data[name]:
            raise ValueError(f"В сценарии нужен путь {name}.")
        directory = (path.parent / data[name]).resolve()
        if (directory == ROOT or not directory.is_relative_to(ROOT)
                or (not allow_incomplete and not directory.is_dir())):
            raise ValueError(f"{name}: нужен каталог внутри проекта.")
        directories.append(directory)
    inputs, reference = directories
    if inputs is not None and reference is not None and (
            inputs.is_relative_to(reference) or reference.is_relative_to(inputs)):
        raise ValueError("Исходные данные и прежний результат должны находиться раздельно.")
    if inputs is not None and inputs.name in (*PERSONAL_MEMORY_ENTRIES, "skills"):
        raise ValueError("Имя входного каталога пересекается с файлами памяти.")
    if not allow_incomplete and not (reference / "answer.md").is_file():
        raise ValueError("Не найден reference/answer.md.")
    metrics = data.get("reference_metrics")
    if metrics is not None and not isinstance(metrics, dict):
        raise ValueError("reference_metrics должен быть объектом или null.")
    history = data.get("history")
    if history is not None:
        if not isinstance(history, str) or not history:
            raise ValueError("history должен быть путём или null.")
        history = (path.parent / history).resolve()
        if not history.is_relative_to(ROOT) or history == ROOT:
            raise ValueError("history должен находиться внутри проекта.")
    notes = data.get("resource_notes", "")
    if not isinstance(notes, str):
        raise ValueError("resource_notes должен быть строкой.")
    return ReplayScenario(data.get("request", ""), inputs, reference, metrics, history, notes)


def assess_replay(proposal: MemoryProposal, scenario: ReplayScenario | None,
                  executable: str, model: str, context: Path | None = None,
                  agent_python: Path | None = None, runner: Callable | None = None) -> dict:
    """Принимает предложение и доступный контекст; возвращает решение LLM run/skip с объяснением.

    Сохраняет сведения сценария, skill, журналы и decision.json в replay-assessment/.
    Задачу не выполняет и исходные данные не копирует. Сбой или неверный протокол
    вызывает исключение, а не разрешение принять память без оценки.
    """
    runner = runner or run_codex
    directory = proposal.run / "replay-assessment"
    directory.mkdir()
    payload = {"scenario_provided": scenario is not None,
               "request": scenario.request if scenario else None,
               "inputs": str(scenario.inputs) if scenario and scenario.inputs else None,
               "reference": str(scenario.reference) if scenario and scenario.reference else None,
               "reference_metrics": scenario.reference_metrics if scenario else None,
               "history": str(scenario.history) if scenario and scenario.history else None,
               "resource_notes": scenario.resource_notes if scenario else "",
               "analysis_context": str(context) if context else None,
               "materials": str(proposal.run / "materials.md"),
               "agent_python": str(agent_python) if agent_python else None}
    information = directory / "scenario.json"
    information.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    skill = ROOT / "agents/assess-replay/skills/assess-replay/SKILL.md"
    instructions = skill.read_text(encoding="utf-8-sig")
    (directory / "SKILL.md").write_text(instructions, encoding="utf-8")
    prompt = (
        instructions + f"\n\nСведения сценария: {information.as_posix()}. "
        "Все пути материалов перечислены в этом JSON; используй их буквально. "
        "Используй только переданные файлы, историю и метаданные. "
        "Не выполняй задачу и запросы к внешним сервисам. Верни JSON по skill."
    )
    reply = runner(executable, model, prompt, directory, directory / "assessment")
    decision = json.loads(reply)
    if (not isinstance(decision, dict) or decision.get("decision") not in ("run", "skip")
            or not isinstance(decision.get("summary"), str) or not decision["summary"].strip()
            or any(not isinstance(decision.get(key), (str, dict, list)) or not decision[key]
                   for key in ("context", "resources"))):
        raise RuntimeError("Неверный протокол оценки возможности прогона.")
    (directory / "decision.json").write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    return decision


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
                       executable: str, model: str, agent_python: Path,
                       user_id: str, backend: str = "deepagents",
                       runner: Callable | None = None) -> dict:
    """Принимает предложение, сценарий, CLI, модель, Python и пользователя; возвращает выбор LLM.

    backend выбирает DeepAgents либо Codex с переданной моделью.
    Один раз запускает решающего агента в копии. Прежние ответы передаются только
    оценщику. Любой сбой вызывает исключение без переноса памяти. Выбор current
    или inconclusive также не является разрешением переноса.
    """
    runner = runner or run_codex
    if backend not in ("deepagents", "codex", "openrouter"):
        raise ValueError("Неизвестный backend проверочного прогона.")
    if scenario.inputs is None or scenario.reference is None or not scenario.request.strip():
        raise ValueError("Для выполнения прогона нужен запрос, inputs и reference.")
    directory = proposal.run / "replay"
    directory.mkdir()
    workspace = directory / "workspace"
    write_snapshot(workspace, proposal.after)
    copy_directory(scenario.inputs, workspace / scenario.inputs.name)
    if (proposal.target / "skills").is_dir():
        copy_directory(proposal.target / "skills", workspace / "skills")
    copy_directory(scenario.reference, directory / "reference")
    (directory / "request.md").write_text(scenario.request, encoding="utf-8")
    (directory / "reference-metrics.json").write_text(
        json.dumps(scenario.reference_metrics, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    command = [
        str(agent_python), "-X", "utf8", str(ROOT / "replay_worker.py"),
        "--workspace", str(workspace), "--request", str(directory / "request.md"),
        "--result", str(directory / "result"), "--user-id", user_id,
    ]
    result = None
    started = perf_counter()
    print(f"Один проверочный прогон: {directory}", flush=True)
    try:
        if backend in ("codex", "openrouter"):
            from replay_codex import run_codex_replay

            run_codex_replay(workspace, directory / "request.md", directory / "result",
                             executable, model, runner=runner, backend=backend)
            result = subprocess.CompletedProcess([], 0)
        else:
            with (directory / "worker.stdout.log").open("w", encoding="utf-8") as stdout:
                with (directory / "worker.stderr.log").open("w", encoding="utf-8") as stderr:
                    result = subprocess.run(command, cwd=ROOT, stdout=stdout, stderr=stderr, timeout=900)
    finally:
        (directory / "execution.json").write_text(json.dumps({
            "backend": backend, "model": model if backend in ("codex", "openrouter") else None,
            "duration_seconds": perf_counter() - started,
            "return_code": result.returncode if result is not None else None,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"Прогон не завершился. См. {directory / 'worker.stderr.log'}")
    metrics = json.loads((directory / "result/metrics.json").read_text(encoding="utf-8"))
    if metrics.get("state") != "completed" or not (directory / "result/answer.md").is_file():
        raise RuntimeError("Прогон не сохранил завершённый результат.")
    if read_memory(workspace, "personal") != proposal.after:
        raise RuntimeError("Агент изменил память во время прогона; перенос отменён.")
    files = directory / "result/files"
    files.mkdir()
    for path in workspace.iterdir():
        if path.name in (*PERSONAL_MEMORY_ENTRIES, "skills"):
            continue
        if path.is_dir():
            copy_directory(path, files / path.name)
        else:
            if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError(f"Ссылка в результате не допускается: {path}")
            shutil.copy2(path, files / path.name)
    skill = ROOT / "agents/evaluate-replay/skills/evaluate-replay/SKILL.md"
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
    reply = runner(executable, model, prompt, directory / "evaluation", directory / "evaluation/judge")
    decision = json.loads(reply)
    if (not isinstance(decision, dict) or decision.get("choice") not in ("candidate", "current", "inconclusive")
            or not isinstance(decision.get("summary"), str) or not decision["summary"].strip()):
        raise RuntimeError("Неверный протокол решения оценщика прогона.")
    (directory / "decision.json").write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    return decision
