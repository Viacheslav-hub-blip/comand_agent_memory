"""Проверка входов и результатов двух штатных сессий без вызовов модели.

Функции: prepare_replay создаёт две рабочие копии, verify_replay проверяет
профили, исходные реплики, сохранность входов и решение судьи.
"""
from pathlib import Path

from command_agent_memory.memory_storage.memory_layout import MemoryLayout
from command_agent_memory.memory_storage.memory_snapshots import read_memory, write_snapshot
from command_agent_memory.worker_agents import validate_worker_profile
from memory_io import hashes, read_json, save_json


def prepare_replay(run: Path, user: dict, state: dict) -> dict:
    """Принимает проверенный снимок и сценарий; создаёт одинаковые входы и возвращает пути рабочих копий."""
    if state["state"] != "reviewed" or not state["replay_requested"]:
        raise ValueError("Для подготовки replay требуется проверенный снимок с запрошенным replay.")
    scenario = user["scenario"]
    inputs = read_memory(scenario.inputs, "knowledge")
    replay = run / "replay"
    if any(Path(name).parts[0] == scenario.inputs.name for name in user["layout"].entries):
        raise ValueError("Область памяти пересекается с входами replay.")
    replay.mkdir()
    for role, snapshot, expected in (("baseline", "before", state["before_sha256"]),
                                     ("candidate", "after", state["snapshot_sha256"])):
        memory = read_memory(run / snapshot, "knowledge")
        if hashes(memory) != expected:
            raise ValueError("Снимок памяти изменился перед replay.")
        workspace = replay / role / "workspace"
        write_snapshot(workspace, memory)
        write_snapshot(workspace / scenario.inputs.name, inputs)
        skills = user["target"] / "skills"
        if skills.is_dir() and not (workspace / "skills").exists():
            write_snapshot(workspace / "skills", read_memory(skills, "knowledge"))
    binding = {"inputs": hashes(inputs), "reference": hashes(read_memory(scenario.reference, "knowledge")),
               "messages": list(scenario.messages), "goal": scenario.goal,
               "layout": state["memory_layout"], "candidate": state["snapshot_sha256"]}
    save_json(replay / "binding.json", binding)
    return {"state": "prepared", "replay": str(replay), "reference": str(scenario.reference),
            "messages": binding["messages"], "goal": scenario.goal}


def verify_replay(run: Path, scenario, before: dict, after: dict) -> None:
    """Принимает журнал, сценарий и снимки; подтверждает сопоставимость и выбор candidate либо вызывает ошибку."""
    replay = run / "replay"
    binding = read_json(replay / "binding.json")
    if (binding["messages"] != list(scenario.messages) or binding["goal"] != scenario.goal
            or binding["inputs"] != hashes(read_memory(scenario.inputs, "knowledge"))
            or binding["reference"] != hashes(read_memory(scenario.reference, "knowledge"))
            or binding["candidate"] != hashes(after)):
        raise ValueError("Сценарий или версия кандидата изменились после подготовки replay.")
    layout = MemoryLayout(tuple(binding["layout"]["personal"]), tuple(binding["layout"]["knowledge"]))
    sessions, profiles = [], []
    for role, snapshot in (("baseline", before), ("candidate", after)):
        directory = replay / role
        workspace = directory / "workspace"
        result = read_json(directory / "result.json")
        session = result["session_id"]
        if not isinstance(session, str) or not session.strip() or not result["profile"]["tools"]:
            raise ValueError("Нужны фактические ID сессии и профиль с инструментами.")
        sessions.append(session)
        profiles.append(validate_worker_profile(result["profile"]))
        if (read_memory(workspace, "personal", layout) != snapshot or
                hashes(read_memory(workspace / scenario.inputs.name, "knowledge")) != binding["inputs"]):
            raise ValueError("Рабочая сессия изменила входы или память.")
        previous = 0
        for turn in result["turns"]:
            index = turn["message_index"]
            if (type(index) is not int or not previous < index <= len(scenario.messages)
                    or (not previous and index != 1) or turn["user"] != scenario.messages[index - 1]
                    or not isinstance(turn["answer"], str) or not turn["answer"].strip()):
                raise ValueError("Реплика или ответ replay не соответствует сценарию.")
            previous = index
        allowed = {"completed"} if role == "candidate" else {"completed", "inconclusive", "turn_limit"}
        if not previous or result["state"] not in allowed:
            raise ValueError("Неполный проверочный диалог.")
    decision = read_json(replay / "decision.json")
    if (sessions[0] == sessions[1] or profiles[0] != profiles[1] or decision.get("choice") != "candidate"
            or not isinstance(decision.get("summary"), str) or not decision["summary"].strip()):
        raise ValueError("Для публикации нужны независимые сопоставимые сессии и решение candidate.")
