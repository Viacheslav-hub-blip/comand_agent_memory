"""Сценарий проверочного диалога, его исходные реплики и проверка наличия данных.

ReplayScenario хранит запрос, входы, прежний результат и измерения.
Функции: load_user_messages читает реплики; load_replay_scenario читает JSON;
check_replay_data возвращает run/skip и список отсутствующих входов.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import re

from command_agent_memory.skill_resources import ROOT
from command_agent_memory.memory_storage.memory_layout import PERSONAL_MEMORY_ENTRIES
from command_agent_memory.worker_agents import validate_agent_spec


@dataclass(frozen=True)
class ReplayScenario:
    """Принимает запрос, пути и измерения; хранит полный или неполный сценарий.

    history указывает на сохранённую историю, resource_notes поясняет затраты.
    messages хранит отдельные пользовательские реплики, goal — цель тестировщика.
    agent подключает штатную фабрику рабочего агента независимо от моделей анализа.
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
    agent: dict | None = None


def load_user_messages(data: dict, scenario_path: Path, project_root: Path) -> tuple[str, ...]:
    """Принимает JSON сценария, его путь и корень проекта; возвращает реплики.

    Форматы messages и dialogue_source задаются по отдельности. Пустой кортеж
    сообщает проверке наличия данных, что исходные реплики не предоставлены.
    """
    if "messages" in data and "dialogue_source" in data:
        raise ValueError("Выберите messages либо dialogue_source.")
    messages = data.get("messages", [])
    if "dialogue_source" in data:
        location = data["dialogue_source"]
        if not isinstance(location, str) or not location.strip():
            raise ValueError("dialogue_source должен содержать путь к исходному чату.")
        source = (scenario_path.parent / location).resolve()
        if not source.is_relative_to(project_root.resolve()) or not source.is_file():
            raise ValueError("Исходный чат должен находиться внутри проекта.")
        messages = re.findall(r"```text\r?\n(.*?)\r?\n```", source.read_text(encoding="utf-8-sig"), re.S)
    if not isinstance(messages, list) or any(not isinstance(item, str) or not item.strip() for item in messages):
        raise ValueError("messages должен содержать список отдельных непустых реплик.")
    if ("messages" in data or "dialogue_source" in data) and not messages:
        raise ValueError("Для диалога нужна хотя бы одна исходная реплика.")
    return tuple(messages)


def load_replay_scenario(path: Path, allow_incomplete: bool = False,
                         allowed_root: Path | None = None) -> ReplayScenario:
    """Принимает путь JSON; возвращает сценарий с абсолютными путями либо вызывает ValueError.

    Пути inputs и reference разрешаются относительно JSON внутри проекта.
    allowed_root задаёт другую допустимую область для подключённого агента.
    В режиме allow_incomplete отсутствие запроса, входов или ответа передаётся
    локальной проверке наличия данных. Типы и границы путей проверяются в обоих режимах.
    """
    path = path.resolve()
    root = (allowed_root or ROOT).resolve()
    if not path.is_file() or not path.is_relative_to(root):
        raise ValueError("Сценарий должен быть JSON-файлом внутри допустимой области.")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("request", ""), str):
        raise ValueError("Сценарий должен быть объектом с текстовым request.")
    messages = load_user_messages(data, path, root)
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
        if (directory == root or not directory.is_relative_to(root)
                or (not allow_incomplete and not directory.is_dir())):
            raise ValueError(f"{name}: нужен каталог внутри допустимой области.")
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
        if not history.is_relative_to(root) or history == root:
            raise ValueError("history должен находиться внутри допустимой области.")
    notes = data.get("resource_notes", "")
    if not isinstance(notes, str):
        raise ValueError("resource_notes должен быть строкой.")
    goal = data.get("goal", "")
    if not isinstance(goal, str):
        raise ValueError("goal должен быть строкой.")
    agent = data.get("agent")
    if agent is not None:
        validate_agent_spec(agent)
    return ReplayScenario(request, inputs, reference, metrics, history, notes, messages, goal, agent)


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
