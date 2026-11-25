"""Диалог тестируемого агента с отдельным агентом, играющим пользователя.

Классы: TesterDecision — строгая структура следующего действия, методы
__post_init__ проверяет поля, model_dump и model_dump_json сериализуют решение;
OpenRouterTester — отдельный контекст тестировщика и его измерения.
Методы OpenRouterTester: __init__, decide.
Функции: __getattr__ загружает ReplayMetrics; message_record сериализует сообщение;
parse_tester_decision разбирает ответ; file_hashes — снимок файлов;
collect_artifacts — сведения о результатах; metrics_record — снимок счётчиков;
advance_dialogue — проверка перехода; run_dialogue — выполнение полного диалога.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import platform
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from command_agent_memory.memory_replay.replay_agent import open_chat_database, run_turn, save_message_record
from command_agent_memory.worker_agents import WorkerSession, load_worker_session, profile_fingerprint
from command_agent_memory.model_execution.stage_runner import StageRuntime
from command_agent_memory.skill_resources import get_skill_path, read_stage_instructions
if TYPE_CHECKING:
    from command_agent_memory.memory_replay.langchain_metrics import ReplayMetrics
    from command_agent_memory.memory_replay.replay_scenario import ReplayScenario


def __getattr__(name: str):
    """Принимает имя атрибута; по запросу загружает прежний экспорт ReplayMetrics."""
    if name == "ReplayMetrics":
        from command_agent_memory.memory_replay.langchain_metrics import ReplayMetrics

        return ReplayMetrics
    raise AttributeError(name)


def message_record(role: str, text: str) -> dict:
    """Принимает тип и текст сообщения; возвращает совместимую запись журнала."""
    return {"type": role, "data": {"type": role, "content": text}}


@dataclass(frozen=True)
class TesterDecision:
    """Принимает действие тестировщика и возвращает проверенную структуру решения.

    send выбирает номер следующей исходной реплики; complete подтверждает цель;
    stop завершает опыт с открытыми вопросами. Нумерация реплик начинается с 1.
    """

    action: Literal["send", "complete", "stop"]
    message_index: int | None
    goal_reached: bool
    summary: str
    evidence: list[str]

    def __post_init__(self):
        """Без дополнительных входов проверяет поля решения; при ошибке вызывает ValueError."""
        if (self.action not in ("send", "complete", "stop")
                or (self.message_index is not None and type(self.message_index) is not int)
                or type(self.goal_reached) is not bool or not isinstance(self.summary, str) or not self.summary
                or not isinstance(self.evidence, list) or not self.evidence
                or any(not isinstance(item, str) for item in self.evidence)):
            raise ValueError("Неверные поля решения тестировщика.")

    def model_dump(self) -> dict:
        """Без входов возвращает поля решения в словаре."""
        return asdict(self)

    def model_dump_json(self) -> str:
        """Без входов возвращает JSON решения для журнала и прежних интеграций."""
        return json.dumps(self.model_dump(), ensure_ascii=False)


def parse_tester_decision(reply: str) -> TesterDecision:
    """Принимает текст модели; возвращает последнее решение строгой схемы.

    Допускает обрамление JSON пояснением или блоком кода. Повреждённый ответ
    вызывает ValueError; число модельных обращений остаётся исходным.
    """
    decoder = json.JSONDecoder()
    decisions = []
    for index, character in enumerate(reply):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(reply, index)
            if isinstance(value, dict):
                decisions.append(TesterDecision(**value))
        except (TypeError, ValueError):
            continue
    if not decisions:
        raise ValueError("Тестировщик вернул ответ за пределами строгой схемы.")
    return decisions[-1]


def file_hashes(root: Path) -> dict[str, str]:
    """Принимает каталог; возвращает относительные пути и SHA-256 всех файлов."""
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Для опыта нужен обычный файл или каталог: {path}")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = sha256(path.read_bytes()).hexdigest()
    return result


def collect_artifacts(root: Path, baseline: dict[str, str], text_limit: int = 120_000) -> list[dict]:
    """Принимает каталог, исходные хеши и лимит текста; возвращает новые результаты.

    Текстовые файлы читаются как UTF-8 в пределах общего лимита символов;
    двоичные представлены размером и хешем. Каждое сокращение явно отмечается.
    """
    artifacts = []
    remaining = text_limit
    for relative, digest in file_hashes(root).items():
        if baseline.get(relative) == digest:
            continue
        raw = (root / relative).read_bytes()
        item = {"path": relative, "bytes": len(raw), "sha256": digest}
        try:
            content = raw.decode("utf-8-sig")
            if "\x00" in content:
                raise UnicodeError("Двоичный файл")
            item.update(text=content[:remaining], truncated=len(content) > remaining)
            remaining = max(0, remaining - len(content))
        except UnicodeError:
            item["binary"] = True
        artifacts.append(item)
    return artifacts


def metrics_record(callback: ReplayMetrics | None, duration: float, *, require_observation: bool = False) -> dict:
    """Принимает счётчики и длительность; возвращает показатели одной роли.

    usage содержит данные провайдера, usage_complete показывает полноту учёта.
    Число обращений модели и вызовов инструментов получено через callbacks.
    require_observation сохраняет null вместо нулей, если внешний агент не передал callbacks.
    """
    if callback is None:
        return {"duration_seconds": duration, "model_calls": None, "tool_calls": None,
                "usage": None, "usage_complete": False}
    observed = not require_observation or callback.model_calls > 0
    return {
        "duration_seconds": duration, "model_calls": callback.model_calls if observed else None,
        "tool_calls": callback.tool_calls if observed else None, "usage": callback.usage_metadata or None,
        "usage_complete": callback.model_calls > 0 and callback.usage_calls == callback.model_calls,
    }


def advance_dialogue(decision: TesterDecision, current: int, message_count: int,
                     turn_count: int, turn_limit: int) -> tuple[str, int | None]:
    """Принимает решение и позицию диалога; возвращает статус и следующую позицию.

    Проверяет подтверждение цели, допустимый номер следующей исходной реплики
    и лимит ходов. Ошибочный протокол вызывает ValueError.
    """
    if decision.action == "complete":
        if not decision.goal_reached or decision.message_index is not None:
            raise ValueError("Завершение требует подтверждённой цели и message_index=null.")
        return "completed", None
    if decision.action == "stop":
        if decision.goal_reached or decision.message_index is not None:
            raise ValueError("Остановка требует открытой цели и message_index=null.")
        return "inconclusive", None
    following = decision.message_index
    if decision.goal_reached or following is None or not current < following <= message_count:
        raise ValueError("Следующая реплика должна продвигать исходный сценарий вперёд.")
    if turn_count >= turn_limit:
        return "turn_limit", None
    return "running", following


class OpenRouterTester:
    """Хранит отдельный контекст пользователя-тестировщика и его показатели.

    Получает цель, все исходные реплики и эталон. Тестируемому агенту передаётся
    выбранная исходная реплика; решения и эталон хранятся в журнале проверяющего.
    """

    def __init__(self, scenario: ReplayScenario, model: str, directory: Path,
                 artifact_text_limit: int = 120_000, runtime=None):
        """Принимает сценарий, модель и журнал; создаёт контекст тестировщика."""
        self.runtime = runtime if runtime is not None else StageRuntime(
            {"stages": {"replay_tester": {"provider": "openrouter", "model": model}}}, directory)
        self.calls = 0
        self.directory = directory
        self.directory.mkdir()
        self.metrics = None
        self.duration = 0.0
        payload = {
            "goal": scenario.goal or "Достичь итогового результата исходного диалога и эталона.",
            "messages": list(scenario.messages),
            "reference": collect_artifacts(scenario.reference, {}, artifact_text_limit),
        }
        rules = read_stage_instructions(get_skill_path("replay-tester"))
        system = rules + "\n\nМатериалы пользователя-тестировщика:\n" + json.dumps(payload, ensure_ascii=False)
        self.messages = [message_record("system", system)]
        (directory / "initialization.md").write_text(system, encoding="utf-8")

    def decide(self, observation: dict) -> TesterDecision:
        """Принимает ответ и результаты текущего хода; возвращает строгое решение."""
        message = message_record("human", json.dumps(observation, ensure_ascii=False))
        self.messages.append(message)
        started = perf_counter()
        with (self.directory / "transcript.jsonl").open("a", encoding="utf-8") as trace:
            trace.write(json.dumps(message, ensure_ascii=False) + "\n")
            try:
                self.calls += 1
                prompt = json.dumps(self.messages, ensure_ascii=False)
                text = self.runtime.run("replay_tester", prompt, self.directory,
                                        self.directory / f"decision-{self.calls:03d}")
                response = message_record("ai", text)
                self.messages.append(response)
                trace.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
                return parse_tester_decision(text)
            finally:
                self.duration += perf_counter() - started


def run_dialogue(workspace: Path, scenario: ReplayScenario, result: Path, model: str,
                 *, user_id: str = "101",
                 tester_model: str | None = None, max_user_turns: int | None = None,
                 recursion_limit: int | None = None, artifact_text_limit: int = 120_000,
                 runtime=None, memory_layout=None, worker_session: WorkerSession | None = None) -> dict:
    """Принимает чистую область и сценарий; сохраняет диалог и раздельные показатели.

    Первый HumanMessage равен первой исходной реплике. Один экземпляр решающего
    агента и его thread_id сохраняются на весь диалог. Тестировщик выбирает
    последующие реплики по ответам. Возвращает metrics.json; сбой сохраняет
    частичный журнал и передаёт исключение вызывающему коду.
    runtime выбирает модели тестировщика и оценщика; worker_session содержит
    штатного агента, который сохраняет контекст всех ходов. memory_layout перечисляет
    защищённые файлы памяти подключённого агента.
    Первое наблюдение тестировщика включает исходные файлы памяти и входов,
    чтобы он мог проверить источник ответа без необоснованного нового уточнения.
    """
    if not scenario.messages or scenario.request != scenario.messages[0]:
        raise ValueError("Для диалога нужны отдельные сообщения и точная первая реплика.")
    if scenario.reference is None or scenario.inputs is None:
        raise ValueError("Для диалога нужны каталоги эталона и исходных данных.")
    limit = len(scenario.messages) if max_user_turns is None else max_user_turns
    if limit < 1 or (recursion_limit is not None and recursion_limit < 1) or artifact_text_limit < 1:
        raise ValueError("Лимиты реплик, шагов и символов должны быть положительными.")
    workspace, result = workspace.resolve(), result.resolve()
    if workspace == result or result.is_relative_to(workspace) or workspace.is_relative_to(result):
        raise ValueError("Рабочая область и журнал должны находиться раздельно.")
    result.mkdir(parents=True, exist_ok=False)
    baseline = file_hashes(workspace)
    initial_context = collect_artifacts(workspace, {}, artifact_text_limit)
    observed_files = baseline
    memory_entries = {"AGENTS.md", "USER_PREFERENCES.md", "user_llm_wiki", "knowledge", "skills", "team_knowledge"}
    protected = {path: value for path, value in baseline.items()
                 if path.split("/")[0] in memory_entries}
    if memory_layout is not None:
        from command_agent_memory.memory_storage.file_storage import FileMemoryStore

        memory_store = FileMemoryStore(workspace, memory_layout.entries)
        protected.update({name: baseline[name] for name in memory_store.snapshot()})
    with ExitStack() as resources:
        if worker_session is None:
            worker_session = load_worker_session(scenario.agent, workspace, memory_layout)
            resources.callback(worker_session.close)
        turn = worker_session.turn
        native_session = turn is not None
        if native_session:
            if runtime is None:
                raise ValueError("Для штатного replay требуется исполнитель этапа тестировщика.")
            protected.update({path: value for path, value in baseline.items()
                              if path.split("/")[0] == scenario.inputs.name})
            callback = None
        else:
            from langchain_core.messages import AIMessage, HumanMessage, message_to_dict
            from command_agent_memory.memory_replay.langchain_metrics import ReplayMetrics

            callback = ReplayMetrics()
        model = worker_session.profile["model"]
        (result / "initialization.json").write_text(json.dumps({
            "model": model,
            "tester_model": runtime.settings["replay_tester"].model if runtime is not None else tester_model or model,
            "memory_enabled": worker_session.profile["environment"].get("memory_enabled"),
            "workspace": str(workspace), "system_prompt": None,
            "system_prompt_source": "host_session" if native_session else "worker_agent_factory",
            "worker_session_id": worker_session.external_id,
            "agent_profile": worker_session.profile, "agent_profile_sha256": profile_fingerprint(worker_session.profile),
            "operating_system": worker_session.profile["environment"].get("os"),
            "python_version": worker_session.profile["environment"].get("python"),
            "host_operating_system": platform.system(), "host_python_version": platform.python_version(),
            "memory_sources": ["/" + path for path in baseline if
                               path in ("AGENTS.md", "USER_PREFERENCES.md", "user_llm_wiki/INDEX.md",
                                        "knowledge/INDEX.md", "team_knowledge/INDEX.md")],
            "skill_sources": ["/" + name + "/" for name in ("skills", "knowledge/skills", "team_knowledge/skills")
                              if (workspace / name).is_dir()],
            "max_user_turns": limit, "recursion_limit": recursion_limit,
            "artifact_text_limit": artifact_text_limit,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        tester = None
        connection = open_chat_database(result / "chat_logs.db")
        session_id = str(uuid4())
        started = perf_counter()
        agent_duration = 0.0
        state, error, answer = "failed", "", ""
        turns = []
        decision = None
        try:
            agent = worker_session.agent
            tester = OpenRouterTester(scenario, tester_model or model, result / "tester",
                                      artifact_text_limit, runtime=runtime)
            message_index = 1
            with (result / "transcript.jsonl").open("w", encoding="utf-8") as trace:
                while True:
                    text = scenario.messages[message_index - 1]
                    user_record = (message_record("human", text) if native_session else
                                   message_to_dict(HumanMessage(content=text)))
                    trace.write(json.dumps(user_record, ensure_ascii=False) + "\n")
                    trace.flush()
                    turn_callback = None if native_session else ReplayMetrics()
                    turn_started = perf_counter()
                    answer = ""
                    try:
                        if native_session:
                            save_message_record(connection, session_id, user_id, user_record)
                            answer = turn(text)
                            answer_record = message_record("ai", answer)
                            save_message_record(connection, session_id, user_id, answer_record)
                            trace.write(json.dumps(answer_record, ensure_ascii=False) + "\n")
                            trace.flush()
                        else:
                            for message in run_turn(agent, text, connection, session_id, user_id,
                                                    callbacks=[callback, turn_callback], recursion_limit=recursion_limit):
                                trace.write(json.dumps(message_to_dict(message), ensure_ascii=False, default=str) + "\n")
                                trace.flush()
                                if isinstance(message, AIMessage) and not message.tool_calls:
                                    answer = message.text
                    finally:
                        duration = perf_counter() - turn_started
                        agent_duration += duration
                        turns.append({"source_message_index": message_index, "request": text,
                                      **metrics_record(turn_callback, duration, require_observation=True)})
                        (result / "turns.json").write_text(json.dumps(turns, ensure_ascii=False, indent=2), encoding="utf-8")
                    if not answer.strip():
                        raise RuntimeError("Тестируемый агент завершил ход с пустым ответом.")
                    (result / "answer.md").write_text(answer, encoding="utf-8")
                    current = file_hashes(workspace)
                    read_only_entries = memory_entries | {scenario.inputs.name} if native_session else memory_entries
                    added_memory = any(path not in baseline and path.split("/")[0] in read_only_entries for path in current)
                    if memory_layout is not None:
                        added_memory = added_memory or any(name not in baseline for name in memory_store.snapshot())
                    if added_memory or any(current.get(path) != digest for path, digest in protected.items()):
                        raise RuntimeError("Обнаружено изменение проверяемой памяти или защищённых входов.")
                    observation = {
                        "turn": len(turns), "last_source_message_index": message_index,
                        "user_message": text, "agent_answer": answer,
                        "artifacts": collect_artifacts(workspace, observed_files, artifact_text_limit),
                        "removed_files": sorted(set(observed_files) - set(current)),
                    }
                    if len(turns) == 1:
                        observation["initial_context"] = initial_context
                    decision = tester.decide(observation)
                    observed_files = current
                    with (result / "decisions.jsonl").open("a", encoding="utf-8") as output:
                        output.write(json.dumps(decision.model_dump(), ensure_ascii=False) + "\n")
                    outcome, following = advance_dialogue(
                        decision, message_index, len(scenario.messages), len(turns), limit,
                    )
                    if following is None:
                        state = outcome
                        break
                    message_index = following
        except BaseException as failure:
            error = f"{type(failure).__name__}: {failure}"
            raise
        finally:
            connection.close()
            metrics = {
                "state": state, "error": error or None, "backend": "dialogue", "model": model,
                "duration_seconds": perf_counter() - started, "user_turns": len(turns),
                **{key: value for key, value in metrics_record(callback, agent_duration, require_observation=True).items()
                   if key != "duration_seconds"},
                "tested_agent": metrics_record(callback, agent_duration, require_observation=True),
                "tester": metrics_record(tester.metrics, tester.duration) if tester else None,
                "decision": decision.model_dump() if decision else None,
            }
            if runtime is not None:
                metrics["tester"] = {"model_calls": None, "tool_calls": None, "usage": None,
                                     "usage_complete": False, "duration_seconds": tester.duration if tester else None}
                metrics["provider_metrics"] = [str(path) for path in sorted(result.rglob("*.metrics.json"))]
            (result / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return metrics
