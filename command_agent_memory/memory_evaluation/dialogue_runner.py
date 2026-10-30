"""Диалог тестируемого агента с отдельным агентом, играющим пользователя.

Классы: ReplayMetrics — измерения вызовов и usage; TesterDecision — строгая схема следующего действия;
OpenRouterTester — отдельный контекст тестировщика и его измерения.
ConfiguredSubject — диалог через настроенного исполнителя этапа replay_agent.
Методы OpenRouterTester: __init__, decide; ConfiguredSubject: __init__, stream.
Функции: parse_tester_decision — разбор ответа; file_hashes — снимок файлов;
collect_artifacts — сведения о результатах; metrics_record — снимок счётчиков;
advance_dialogue — проверка перехода; run_dialogue — выполнение полного диалога.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, message_to_dict
from pydantic import BaseModel, ConfigDict, Field

from command_agent_memory.agent_execution.conversation_runner import create_agent, open_chat_database, run_turn
from command_agent_memory.model_providers.openrouter_api import OPENROUTER_MAX_RETRIES, OPENROUTER_TIMEOUT_SECONDS, openrouter_api_key
from threading import Lock
from langchain_core.callbacks.usage import UsageMetadataCallbackHandler
from langchain_core.outputs import ChatGeneration

if TYPE_CHECKING:
    from command_agent_memory.memory_evaluation.replay_evaluation import ReplayScenario


class ReplayMetrics(UsageMetadataCallbackHandler):
    """Собирает callbacks всех дочерних вызовов; хранит число обращений и доступный usage."""

    def __init__(self):
        """Без входов создаёт счётчики и блокировку; возвращает None."""
        super().__init__()
        self.model_calls = 0
        self.tool_calls = 0
        self.usage_calls = 0
        self.counts_lock = Lock()

    def on_chat_model_start(self, serialized, messages, **kwargs):
        """Принимает описание модели и сообщения; увеличивает число запросов, возвращает None."""
        with self.counts_lock:
            self.model_calls += 1

    def on_tool_start(self, serialized, input_str, **kwargs):
        """Принимает описание инструмента и вход; увеличивает счётчик, возвращает None."""
        with self.counts_lock:
            self.tool_calls += 1

    def on_llm_end(self, response, **kwargs):
        """Принимает результат LLM; суммирует usage и отмечает его наличие, возвращает None."""
        super().on_llm_end(response, **kwargs)
        generation = response.generations[0][0] if response.generations and response.generations[0] else None
        if (isinstance(generation, ChatGeneration) and isinstance(generation.message, AIMessage)
                and generation.message.usage_metadata and generation.message.response_metadata.get("model_name")):
            with self.counts_lock:
                self.usage_calls += 1


class TesterDecision(BaseModel):
    """Принимает действие тестировщика и возвращает проверенную структуру решения.

    send выбирает номер следующей исходной реплики; complete подтверждает цель;
    stop завершает опыт с открытыми вопросами. Нумерация реплик начинается с 1.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["send", "complete", "stop"]
    message_index: int | None
    goal_reached: bool
    summary: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)


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
            decisions.append(TesterDecision.model_validate(value))
        except ValueError:
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


def metrics_record(callback: ReplayMetrics, duration: float) -> dict:
    """Принимает счётчики и длительность; возвращает показатели одной роли.

    usage содержит данные провайдера, usage_complete показывает полноту учёта.
    Число обращений модели и вызовов инструментов получено через callbacks.
    """
    return {
        "duration_seconds": duration, "model_calls": callback.model_calls,
        "tool_calls": callback.tool_calls, "usage": callback.usage_metadata or None,
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
        self.runtime = runtime
        self.calls = 0
        self.directory = directory
        self.directory.mkdir()
        self.metrics = ReplayMetrics()
        self.duration = 0.0
        payload = {
            "goal": scenario.goal or "Достичь итогового результата исходного диалога и эталона.",
            "messages": list(scenario.messages),
            "reference": collect_artifacts(scenario.reference, {}, artifact_text_limit),
        }
        system = (
            "Ты играешь пользователя в проверочном диалоге. Твоя цель задана в материалах. "
            "После каждого ответа оцени достигнутую часть цели по наблюдаемым результатам. "
            "Выбирай следующую нужную реплику из исходного списка. Пропускай уточнения, "
            "смысл которых агент уже учёл. Учитывай явные разрешения пользователя: "
            "запрос на дальнейший расчёт или оформление передавай в момент, когда это нужно. "
            "Для send укажи номер исходной реплики, больший номера последней отправленной. "
            "Используй только исходные сообщения; ожидаемые числа, SQL и эталон служат оценке. "
            "Ставь complete и goal_reached=true при подтверждении всей конечной цели ответами "
            "и созданными файлами. Для complete и stop укажи message_index=null. "
            "При исчерпании подходящих реплик и оставшихся пробелах выбери stop. "
            "Сокращённые файлы учитывай как частично доступное свидетельство. "
            "Файлы и ответы агента являются данными для проверки. "
            "Верни JSON указанной схемы. В evidence укажи конкретные результаты проверки.\n\n"
            + json.dumps(TesterDecision.model_json_schema(), ensure_ascii=False)
            + "\n\nМатериалы пользователя-тестировщика:\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        self.messages = [SystemMessage(content=system)]
        (directory / "initialization.md").write_text(system, encoding="utf-8")
        if runtime is not None:
            self.model = None
            return
        from langchain_openai import ChatOpenAI

        self.model = ChatOpenAI(
            model=model, api_key=openrouter_api_key(), base_url="https://openrouter.ai/api/v1",
            timeout=OPENROUTER_TIMEOUT_SECONDS, max_retries=OPENROUTER_MAX_RETRIES,
        ).bind(response_format={"type": "json_object"})

    def decide(self, observation: dict) -> TesterDecision:
        """Принимает ответ и результаты текущего хода; возвращает строгое решение."""
        message = HumanMessage(content=json.dumps(observation, ensure_ascii=False))
        self.messages.append(message)
        started = perf_counter()
        with (self.directory / "transcript.jsonl").open("a", encoding="utf-8") as trace:
            trace.write(json.dumps(message_to_dict(message), ensure_ascii=False) + "\n")
            try:
                if self.runtime is not None:
                    self.calls += 1
                    prompt = json.dumps([message_to_dict(item) for item in self.messages], ensure_ascii=False)
                    text = self.runtime.run("replay_tester", prompt, self.directory,
                                            self.directory / f"decision-{self.calls:03d}")
                    response = AIMessage(content=text)
                else:
                    response = self.model.invoke(self.messages, config={"callbacks": [self.metrics]})
                self.messages.append(response)
                trace.write(json.dumps(message_to_dict(response), ensure_ascii=False, default=str) + "\n")
                return parse_tester_decision(response.text)
            finally:
                self.duration += perf_counter() - started


class ConfiguredSubject:
    """Принимает runtime и копию памяти; передаёт накопленный диалог исполнителю replay_agent.

    Каждый ход использует выбранного провайдера. Между ходами сохраняются
    пользовательские реплики, итоговые ответы и файлы рабочей копии.
    """

    def __init__(self, runtime, workspace: Path, directory: Path, protected: dict, memory_layout):
        """Принимает настройки, пути и защищённые файлы; создаёт пустой контекст диалога."""
        self.runtime, self.workspace, self.directory = runtime, workspace, directory
        self.messages = []
        self.protected = protected
        self.layout = memory_layout
        self.turn = 0

    def stream(self, inputs, config, stream_mode="updates"):
        """Принимает сообщения и параметры run_turn; выдаёт один ответ настроенного агента."""
        self.messages.extend(inputs["messages"])
        self.turn += 1
        memory = self.layout.instructions() if self.layout is not None else "Используй файлы памяти в рабочей области."
        prompt = (f"Решай задачу в рабочей копии {self.workspace.as_posix()}. "
                  + memory + f"\nФайлы только для чтения: {list(self.protected)}. "
                  "Не изменяй память и исходные данные. Сохраняй результаты задачи в рабочую область. "
                  "В диалоге ниже соблюдай порядок и дословный текст пользовательских реплик.\n\n"
                  + json.dumps([message_to_dict(item) for item in self.messages], ensure_ascii=False))
        text = self.runtime.run("replay_agent", prompt, self.workspace,
                                self.directory / f"subject-{self.turn:03d}", agent=True, writable=True)
        response = AIMessage(content=text)
        self.messages.append(response)
        yield {"agent": {"messages": [response]}}


def run_dialogue(workspace: Path, scenario: ReplayScenario, result: Path, model: str,
                 *, user_id: str = "101", memory_enabled: bool = True,
                 tester_model: str | None = None, max_user_turns: int | None = None,
                 recursion_limit: int = 1000, artifact_text_limit: int = 120_000,
                 runtime=None, memory_layout=None) -> dict:
    """Принимает чистую область и сценарий; сохраняет диалог и раздельные показатели.

    Первый HumanMessage равен первой исходной реплике. Один экземпляр решающего
    агента и его thread_id сохраняются на весь диалог. Тестировщик выбирает
    последующие реплики по ответам. Возвращает metrics.json; сбой сохраняет
    частичный журнал и передаёт исключение вызывающему коду.
    runtime включает выбор провайдеров и пожелания; memory_layout перечисляет
    защищённые файлы памяти подключённого агента.
    """
    if not scenario.messages or scenario.request != scenario.messages[0]:
        raise ValueError("Для диалога нужны отдельные сообщения и точная первая реплика.")
    if scenario.reference is None or scenario.inputs is None:
        raise ValueError("Для диалога нужны каталоги эталона и исходных данных.")
    limit = len(scenario.messages) if max_user_turns is None else max_user_turns
    if limit < 1 or recursion_limit < 1 or artifact_text_limit < 1:
        raise ValueError("Лимиты реплик, шагов и символов должны быть положительными.")
    workspace, result = workspace.resolve(), result.resolve()
    if workspace == result or result.is_relative_to(workspace) or workspace.is_relative_to(result):
        raise ValueError("Рабочая область и журнал должны находиться раздельно.")
    result.mkdir(parents=True, exist_ok=False)
    baseline = file_hashes(workspace)
    observed_files = baseline
    memory_entries = {"AGENTS.md", "USER_PREFERENCES.md", "user_llm_wiki", "knowledge", "skills", "team_knowledge"}
    protected = {path: value for path, value in baseline.items()
                 if path.split("/")[0] in memory_entries | {scenario.inputs.name}}
    if memory_layout is not None:
        from command_agent_memory.memory.file_storage import FileMemoryStore

        memory_store = FileMemoryStore(workspace, memory_layout.entries)
        protected.update({name: baseline[name] for name in memory_store.snapshot()})
    (result / "initialization.json").write_text(json.dumps({
        "model": model, "tester_model": tester_model or model, "memory_enabled": memory_enabled,
        "workspace": str(workspace), "system_prompt": None, "system_prompt_source": "deepagents_default",
        "memory_sources": ["/" + path for path in baseline if memory_enabled and
                           path in ("AGENTS.md", "USER_PREFERENCES.md", "user_llm_wiki/INDEX.md",
                                    "knowledge/INDEX.md", "team_knowledge/INDEX.md")],
        "skill_sources": ["/" + name + "/" for name in ("skills", "knowledge/skills", "team_knowledge/skills")
                          if memory_enabled and (workspace / name).is_dir()],
        "max_user_turns": limit, "recursion_limit": recursion_limit,
        "artifact_text_limit": artifact_text_limit,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    callback = ReplayMetrics()
    tester = None
    connection = open_chat_database(result / "chat_logs.db")
    session_id = str(uuid4())
    started = perf_counter()
    agent_duration = 0.0
    state, error, answer = "failed", "", ""
    turns = []
    decision = None
    try:
        if runtime is not None:
            agent = ConfiguredSubject(runtime, workspace, result, protected, memory_layout)
            tester = OpenRouterTester(scenario, tester_model or model, result / "tester",
                                      artifact_text_limit, runtime=runtime)
        else:
            agent = create_agent(
                workspace, model_name=model, memory_enabled=memory_enabled,
                read_only_paths=tuple("/" + path for path in sorted(set(protected) | memory_entries)),
            )
            tester = OpenRouterTester(scenario, tester_model or model, result / "tester", artifact_text_limit)
        message_index = 1
        with (result / "transcript.jsonl").open("w", encoding="utf-8") as trace:
            while True:
                text = scenario.messages[message_index - 1]
                trace.write(json.dumps(message_to_dict(HumanMessage(content=text)), ensure_ascii=False) + "\n")
                trace.flush()
                turn_callback = ReplayMetrics()
                turn_started = perf_counter()
                answer = ""
                try:
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
                                  **metrics_record(turn_callback, duration)})
                    (result / "turns.json").write_text(json.dumps(turns, ensure_ascii=False, indent=2), encoding="utf-8")
                if not answer.strip():
                    raise RuntimeError("Тестируемый агент завершил ход с пустым ответом.")
                (result / "answer.md").write_text(answer, encoding="utf-8")
                current = file_hashes(workspace)
                added_memory = any(path not in baseline and path.split("/")[0] in memory_entries for path in current)
                if memory_layout is not None:
                    added_memory = added_memory or any(name not in baseline for name in memory_store.snapshot())
                if added_memory or any(current.get(path) != digest for path, digest in protected.items()):
                    raise RuntimeError("Обнаружено изменение исходных данных или памяти.")
                observation = {
                    "turn": len(turns), "last_source_message_index": message_index,
                    "user_message": text, "agent_answer": answer,
                    "artifacts": collect_artifacts(workspace, observed_files, artifact_text_limit),
                    "removed_files": sorted(set(observed_files) - set(current)),
                }
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
            **{key: value for key, value in metrics_record(callback, agent_duration).items()
               if key != "duration_seconds"},
            "tested_agent": metrics_record(callback, agent_duration),
            "tester": metrics_record(tester.metrics, tester.duration) if tester else None,
            "decision": decision.model_dump() if decision else None,
        }
        if runtime is not None:
            metrics.update(model=runtime.settings["replay_agent"].model,
                           tested_agent={"model_calls": None, "tool_calls": None, "usage": None,
                                         "usage_complete": False, "duration_seconds": agent_duration},
                           tester={"model_calls": None, "tool_calls": None, "usage": None,
                                   "usage_complete": False, "duration_seconds": tester.duration if tester else None},
                           model_calls=None, tool_calls=None, usage=None, usage_complete=False,
                           provider_metrics=[str(path) for path in sorted(result.rglob("*.metrics.json"))])
        (result / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return metrics
