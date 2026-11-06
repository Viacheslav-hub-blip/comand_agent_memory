"""Парные прогоны штатного агента с прежней и предложенной памятью.

Функции: is_replay_decision проверяет схему; parse_replay_decision читает решение;
copy_directory копирует входы; build_evaluation_packet включает основные
артефакты прямо в запрос; prepare_workspace готовит копию; collect_result сохраняет
результаты; evaluate_by_replay выполняет два диалога через одну фабрику
и возвращает выбор candidate/current/inconclusive без публикации памяти.
"""
from contextlib import ExitStack
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
from command_agent_memory.model_execution.execution_policy import execution_policy
from command_agent_memory.worker_agents import load_worker_session, require_same_profile, validate_agent_spec


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


def build_evaluation_packet(directory: Path, scenario: ReplayScenario, text_limit: int = 120_000) -> str:
    """Принимает каталог replay, сценарий и бюджет текста; возвращает пакет основных материалов.

    Включает ответы, измерения, пользовательские реплики, входы и артефакты.
    Для больших и бинарных файлов сохраняет путь и размер, не выдавая усечение
    за полный документ. JSONL-трассы остаются доступны по путям для точечной проверки.
    """
    if text_limit < 1:
        raise ValueError("Бюджет текста оценки должен быть положительным.")
    selected = [directory / name for name in (
        "request.md", "reference/answer.md", "result/answer.md", "reference-metrics.json",
        "result/metrics.json", "result/turns.json", "baseline/result/answer.md",
        "baseline/result/metrics.json", "baseline/result/turns.json", "comparison.json",
        "reference/correctness-criteria.md",
    )]
    for root in (scenario.inputs, directory / "reference/files", directory / "baseline/result/files", directory / "result/files"):
        if root is not None and root.is_dir():
            selected.extend(sorted(path for path in root.rglob("*") if path.is_file()))
    documents = []
    remaining = text_limit
    for path in dict.fromkeys(selected):
        if not path.is_file():
            continue
        item = {"path": path.as_posix(), "size_bytes": path.stat().st_size}
        if item["size_bytes"] > remaining:
            item["omitted"] = "Размер превышает оставшийся бюджет; файл доступен по пути."
        else:
            try:
                item["text"] = path.read_text(encoding="utf-8-sig")
                remaining -= item["size_bytes"]
            except UnicodeDecodeError:
                item["omitted"] = "Бинарный файл; доступен по пути."
        documents.append(item)
    return json.dumps({"messages": scenario.messages, "goal": scenario.goal,
                       "documents": documents}, ensure_ascii=False)


def prepare_workspace(proposal: MemoryProposal, scenario: ReplayScenario,
                      destination: Path, memory: dict[str, bytes]) -> None:
    """Принимает предложение, сценарий, каталог и снимок памяти; готовит независимые входы задачи."""
    if proposal.layout is not None and any(
            Path(scenario.inputs.name).is_relative_to(Path(name))
            or Path(name).is_relative_to(Path(scenario.inputs.name)) for name in proposal.layout.entries):
        raise ValueError("Входные файлы прогона пересекаются с настроенной памятью.")
    write_snapshot(destination, memory)
    copy_directory(scenario.inputs, destination / scenario.inputs.name)
    if (proposal.target / "skills").is_dir() and not (destination / "skills").exists():
        copy_directory(proposal.target / "skills", destination / "skills")


def collect_result(proposal: MemoryProposal, workspace: Path, result: Path,
                   memory: dict[str, bytes], *, candidate: bool) -> None:
    """Принимает прогон и ожидаемую память; проверяет завершение и сохраняет результаты задачи.

    Незавершённый baseline может показать недостаток прежней памяти. Для candidate
    требуется completed. Изменение проверяемой памяти всегда останавливает публикацию.
    """
    metrics = json.loads((result / "metrics.json").read_text(encoding="utf-8"))
    allowed = ("completed",) if candidate else ("completed", "inconclusive", "turn_limit")
    if metrics.get("state") not in allowed or not (result / "answer.md").is_file():
        raise RuntimeError("Прогон не сохранил завершённый результат.")
    if read_memory(workspace, "personal", proposal.layout) != memory:
        raise RuntimeError("Агент изменил память во время прогона; перенос отменён.")
    files = result / "files"
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


def evaluate_by_replay(proposal: MemoryProposal, scenario: ReplayScenario,
                       executable: str, model: str, user_id: str,
                       runner: Callable | None = None, runtime=None) -> dict:
    """Принимает память, сценарий, модель оценщика и владельца; возвращает выбор версии.

    Две новые сессии одной штатной фабрики решают задачу с before/after. Модель,
    фактические инструменты и условия обязаны совпадать. Исторический ответ служит
    отдельным материалом проверки качества. Ошибка настройки не заменяет рабочего
    агента универсальной моделью и не разрешает публикацию памяти.
    """
    from command_agent_memory.memory_replay.dialogue_runner import run_dialogue

    runner = runner or run_codex
    if scenario.inputs is None or scenario.reference is None or not scenario.request.strip():
        raise ValueError("Для выполнения прогона нужен запрос, inputs и reference.")
    if not scenario.messages:
        raise ValueError("Для диалогового прогона нужны отдельные messages либо dialogue_source.")
    validate_agent_spec(scenario.agent)
    directory = proposal.run / "replay"
    directory.mkdir()
    baseline_workspace = directory / "baseline/workspace"
    workspace = directory / "workspace"
    prepare_workspace(proposal, scenario, baseline_workspace, proposal.before)
    prepare_workspace(proposal, scenario, workspace, proposal.after)
    from command_agent_memory.memory_replay.dialogue_runner import file_hashes

    input_hashes = file_hashes(baseline_workspace / scenario.inputs.name)
    if input_hashes != file_hashes(workspace / scenario.inputs.name):
        raise RuntimeError("Исходные данные изменились между подготовкой двух прогонов.")
    copy_directory(scenario.reference, directory / "reference")
    (directory / "request.md").write_text(scenario.request, encoding="utf-8")
    (directory / "reference-metrics.json").write_text(json.dumps(
        scenario.reference_metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    completed = False
    started = perf_counter()
    try:
        with ExitStack() as resources:
            baseline_session = load_worker_session(scenario.agent, baseline_workspace, proposal.layout)
            resources.callback(baseline_session.close)
            candidate_session = load_worker_session(scenario.agent, workspace, proposal.layout)
            resources.callback(candidate_session.close)
            fingerprint = require_same_profile(baseline_session, candidate_session)
            (directory / "agent-profile.json").write_text(json.dumps(
                baseline_session.profile, ensure_ascii=False, indent=2), encoding="utf-8")
            (directory / "comparison.json").write_text(json.dumps(
                {"agent_profile_sha256": fingerprint, "inputs_sha256": input_hashes,
                 "baseline": "baseline/result", "candidate": "result", "historical_reference": "reference"},
                ensure_ascii=False, indent=2), encoding="utf-8")
            for candidate, current_workspace, result, session, memory in (
                    (False, baseline_workspace, directory / "baseline/result", baseline_session, proposal.before),
                    (True, workspace, directory / "result", candidate_session, proposal.after)):
                run_dialogue(current_workspace, scenario, result, session.profile["model"], user_id=user_id,
                             runtime=runtime, memory_layout=proposal.layout, worker_session=session)
                collect_result(proposal, current_workspace, result, memory, candidate=candidate)
                if require_same_profile(baseline_session, candidate_session) != fingerprint:
                    raise RuntimeError("Профиль агента изменился во время проверки.")
            completed = True
    finally:
        (directory / "execution.json").write_text(json.dumps(
            {"backend": "paired_dialogue", "completed": completed,
             "duration_seconds": perf_counter() - started}, ensure_ascii=False, indent=2), encoding="utf-8")
    skill = get_skill_path("replay-evaluation")
    (directory / "evaluation").mkdir()
    prompt = (skill.read_text(encoding="utf-8")
              + "\n\nСравни два свежих запуска штатного рабочего агента: current — baseline/result, "
              "candidate — result. Исторический reference — отдельный материал проверки качества; "
              "его затраты не заменяют измерения свежего baseline. "
              "Профили модели, инструментов и среды проверены Python-кодом. "
              "Основные материалы включены ниже. Дополнительные трассы доступны в "
              + directory.as_posix() + ". Верни решение по skill.\n\n"
              + build_evaluation_packet(directory, scenario))
    judge_runner = runtime.runner("evaluation") if runtime is not None else runner
    with execution_policy(write_paths=()):
        reply = judge_runner(executable, model, prompt, directory / "evaluation", directory / "evaluation/judge")
    decision = parse_replay_decision(reply)
    (directory / "decision.json").write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    return decision
