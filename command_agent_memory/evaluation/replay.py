"""Один прогон задачи с предложенной памятью и сравнение результатов через LLM.

Вход: предложение памяти, сценарий, Python решающего агента и Codex оценщика.
Выход: копия окружения, результат задачи, измерения, журналы и решение оценщика.
Класс ReplayScenario хранит запрос, пути входов и прежнего результата, измерения.
Функции: load_replay_scenario — чтение сценария; check_replay_data — наличие данных;
assess_replay — прежняя оценка стоимости для отдельных экспериментов;
copy_directory — копирование; is_replay_decision — проверка схемы решения;
parse_replay_decision — извлечение строгого решения из ответа;
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
from command_agent_memory.evaluation.scenario import load_user_messages


@dataclass(frozen=True)
class ReplayScenario:
    """Принимает запрос, пути и измерения; хранит полный или неполный сценарий.

    history указывает на сохранённую историю, resource_notes поясняет затраты.
    messages хранит отдельные пользовательские реплики, goal — цель тестировщика.
    Отсутствующие inputs/reference допустимы для оценки, но не для выполнения.
    """

    request: str
    inputs: Path | None
    reference: Path | None
    reference_metrics: dict | None = None
    history: Path | None = None
    resource_notes: str = ""
    messages: tuple[str, ...] = ()
    goal: str = ""


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
    messages = load_user_messages(data, path, ROOT)
    request = data.get("request", "") or (messages[0] if messages else "")
    if messages and request != messages[0]:
        raise ValueError("request должен совпадать с первой исходной репликой.")
    if not allow_incomplete and not request.strip():
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
    goal = data.get("goal", "")
    if not isinstance(goal, str):
        raise ValueError("goal должен быть строкой.")
    return ReplayScenario(request, inputs, reference, metrics, history, notes, messages, goal)


def check_replay_data(scenario: ReplayScenario | None) -> dict:
    """Принимает сценарий либо None; возвращает run/skip и недостающие данные.

    Выполняет локальную проверку входов диалогового прогона. Стоимость и
    целесообразность не оцениваются моделью. Ошибки структуры не превращаются
    в отсутствие данных: их проверяет загрузчик сценария.
    """
    missing = []
    if scenario is None:
        missing.append("Не передан сценарий диалоговой проверки")
    else:
        if not scenario.request.strip():
            missing.append("Нет исходного запроса")
        if not scenario.messages:
            missing.append("Нет отдельных исходных пользовательских реплик")
        elif scenario.request != scenario.messages[0]:
            raise ValueError("request должен совпадать с первой исходной репликой.")
        if scenario.inputs is None or not scenario.inputs.is_dir():
            missing.append("Нет каталога исходных файлов")
        if scenario.reference is None or not scenario.reference.is_dir():
            missing.append("Нет каталога прежнего результата")
        elif not (scenario.reference / "answer.md").is_file():
            missing.append("Нет прежнего ответа reference/answer.md")
        elif not (scenario.reference / "answer.md").read_text(encoding="utf-8-sig").strip():
            missing.append("Прежний ответ пуст")
    return {"decision": "skip" if missing else "run", "missing": missing,
            "summary": "; ".join(missing) if missing else "Данные для диалогового прогона доступны."}


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
    if backend not in ("deepagents", "codex", "openrouter", "dialogue"):
        raise ValueError("Неизвестный backend проверочного прогона.")
    if scenario.inputs is None or scenario.reference is None or not scenario.request.strip():
        raise ValueError("Для выполнения прогона нужен запрос, inputs и reference.")
    if backend == "dialogue" and not scenario.messages:
        raise ValueError("Для диалогового прогона нужны отдельные messages либо dialogue_source.")
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
        if backend == "dialogue":
            from command_agent_memory.evaluation.dialogue import run_dialogue

            run_dialogue(workspace, scenario, directory / "result", model,
                         user_id=user_id, memory_enabled=True)
            result = subprocess.CompletedProcess([], 0)
        elif backend in ("codex", "openrouter"):
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
            "backend": backend, "model": model if backend in ("codex", "openrouter", "dialogue") else None,
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
    decision = parse_replay_decision(reply)
    (directory / "decision.json").write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    return decision
