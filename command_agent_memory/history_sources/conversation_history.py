"""Адаптеры историй Codex, Claude Code, GigaAgent, Phoenix и произвольных JSONL/SQLite.

Классы: HistoryEvent — общее событие; HistorySource — контракт источника;
RecordSource — чтение JSONL/SQLite; ClaudeSource — транскрипты Claude Code;
CodexSource — rollout; GigaAgentSource — выбранная ветка checkpoints;
PhoenixSource — spans OpenInference.
SQLSource подключается лениво для чтения SQL через внешний DB-API драйвер.
Функции: read_jsonl — строгое чтение; attribute — доступ к атрибутам;
request_json — запрос API; remove_embedded_binary — очистка бинарных данных;
project_name — нормализация имени проекта; register_history_source — регистрация;
load_history_source — фабрика; export_events — запись общей истории.
Методы источников: __init__, records, list_sessions, iter_events, paths, history,
session_id. Вложенная records в read_jsonl проверяет строки JSONL.
"""
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
import io
import json
import os
from pathlib import Path, PureWindowsPath
import sqlite3
from typing import Iterator, Protocol
from urllib.parse import urlencode, quote, urlsplit
from urllib.request import Request, urlopen



def remove_embedded_binary(value):
    """Принимает JSON-значение; возвращает копию без строк data URI с base64.

    Словари и списки обходятся рекурсивно. Обычный текст сохраняется без изменений.
    Вместо бинарной части остаётся тип данных и число удалённых символов.
    """
    if isinstance(value, dict):
        return {key: remove_embedded_binary(item) for key, item in value.items()}
    if isinstance(value, list):
        return [remove_embedded_binary(item) for item in value]
    if isinstance(value, str) and value.startswith("data:") and ";base64," in value:
        header, encoded = value.split(",", 1)
        return f"[{header}; удалено {len(encoded)} символов base64]"
    return value


def project_name(cwd: str) -> str:
    """Принимает cwd из индекса; возвращает имя проекта для Windows или POSIX."""
    return (PureWindowsPath(cwd).name if '\\' in cwd else Path(cwd).name).casefold()


@contextmanager
def read_jsonl(path: Path):
    """Принимает JSONL либо JSONL.zst; выдаёт итератор объектов, ошибки не пропускает."""
    from contextlib import ExitStack

    def records(stream):
        """Принимает текстовый поток; выдаёт JSON-объекты с диагностикой номера строки."""
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("Ожидался JSON-объект")
            except ValueError as error:
                raise ValueError(f"Повреждённый лог {path}, строка {number}: {error}") from error
            yield value

    with ExitStack() as stack:
        raw = stack.enter_context(path.open("rb"))
        if path.suffix == ".zst":
            try:
                import zstandard
            except ImportError as error:
                raise RuntimeError("Для .jsonl.zst установите zstandard.") from error
            raw = stack.enter_context(zstandard.ZstdDecompressor().stream_reader(raw))
        stream = stack.enter_context(io.TextIOWrapper(raw, encoding="utf-8-sig"))
        yield records(stream)


def attribute(value: dict, name: str, default=None):
    """Принимает объект и имя через точки; возвращает плоский либо вложенный атрибут."""
    if name in value:
        return value[name]
    current = value
    for part in name.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def request_json(base_url: str, route: str, *, token_env: str | None = None, body=None):
    """Принимает URL, маршрут, имя переменной токена и тело; возвращает JSON ответа API."""
    parsed = urlsplit(base_url)
    if (parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("Нужен http(s) URL без учётных данных, query и fragment.")
    headers = {"Accept": "application/json"}
    if token_env:
        token = os.environ.get(token_env)
        if not token:
            raise ValueError(f"Не задана переменная токена {token_env}.")
        headers["Authorization"] = "Bearer " + token
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    request = Request(base_url.rstrip("/") + "/" + route.lstrip("/"), data=encoded, headers=headers)
    with urlopen(request, timeout=60) as response:
        return json.load(response)


@dataclass(frozen=True)
class HistoryEvent:
    """Принимает источник, сессию, тип и исходные данные; хранит событие без потери полей."""

    source: str
    session_id: str
    event_id: str
    kind: str
    data: dict
    timestamp: str | None = None
    parent_id: str | None = None
    metadata: dict = field(default_factory=dict)


class HistorySource(Protocol):
    """Описывает источник: список сессий и поток событий выбранной сессии."""

    def list_sessions(self) -> list[dict]:
        """Без входов возвращает метаданные сессий с обязательным строковым id."""
        ...

    def iter_events(self, session: dict) -> Iterator[HistoryEvent]:
        """Принимает метаданные сессии; выдаёт полные события общего формата."""
        ...


class RecordSource:
    """Принимает конфигурацию JSONL/SQLite; группирует записи по указанному полю сессии."""

    def __init__(self, options: dict, base: Path):
        """Принимает параметры и базовый путь; сохраняет источник без записи в него."""
        self.options = options
        self.base = base
        self.name = options["type"]

    def records(self) -> Iterator[dict]:
        """Без входов выдаёт исходные записи JSONL либо результаты SELECT в SQLite."""
        path = self.base / Path(self.options["path"]).expanduser()
        if self.name == "sqlite":
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                query = self.options.get("query", "SELECT * FROM messages ORDER BY id")
                for row in connection.execute(query, self.options.get("parameters", {})):
                    yield dict(row)
        else:
            with read_jsonl(path) as records:
                yield from records

    def list_sessions(self) -> list[dict]:
        """Без входов возвращает уникальные сессии с учётом выбранного user_id."""
        sessions = {}
        key = self.options.get("session_field", "session_id")
        for record in self.records():
            user = self.options.get("user_id")
            if user is not None and str(attribute(record, self.options.get("user_field", "user_id"))) != str(user):
                continue
            identity = attribute(record, key)
            if identity is None or not str(identity):
                raise ValueError(f"В записи {self.name} нет поля сессии {key}.")
            sessions.setdefault(str(identity), {"id": str(identity)})
        return list(sessions.values())

    def iter_events(self, session: dict) -> Iterator[HistoryEvent]:
        """Принимает сессию; выдаёт её исходные записи с метаданными общего формата."""
        key = self.options.get("session_field", "session_id")
        for number, record in enumerate(self.records(), 1):
            if str(attribute(record, key)) != session["id"]:
                continue
            user = self.options.get("user_id")
            if user is not None and str(attribute(record, self.options.get("user_field", "user_id"))) != str(user):
                continue
            yield HistoryEvent(self.name, session["id"], str(record.get("id", number)),
                               str(record.get("type", record.get("role", "record"))), record,
                               record.get("timestamp", record.get("created_at")))


class ClaudeSource(RecordSource):
    """Читает локальные транскрипты Claude Code; сохраняет дерево сообщений и content blocks."""

    def paths(self) -> list[Path]:
        """Без входов возвращает основные JSONL-файлы из файла или каталога projects."""
        root = self.base / Path(self.options["path"]).expanduser()
        if root.is_file():
            return [root]
        if not root.is_dir():
            raise FileNotFoundError(root)
        return sorted(path for path in root.rglob("*.jsonl") if "subagents" not in path.relative_to(root).parts)

    def list_sessions(self) -> list[dict]:
        """Без входов возвращает сессии; проект определяется по cwd, а не имени каталога."""
        result = []
        for path in self.paths():
            metadata = None
            with read_jsonl(path) as records:
                for record in records:
                    if record.get("sessionId") and record.get("cwd"):
                        metadata = {"id": record["sessionId"], "path": str(path),
                                    "project": project_name(record["cwd"]), "cwd": record["cwd"],
                                    "version": record.get("version")}
                        break
            if metadata:
                result.append(metadata)
        return result

    def iter_events(self, session: dict) -> Iterator[HistoryEvent]:
        """Принимает сессию; выдаёт основные события и отдельные ветки субагентов."""
        main = Path(session["path"])
        paths = [main]
        if self.options.get("include_subagents", True):
            paths.extend(sorted((main.parent / main.stem / "subagents").rglob("*.jsonl")))
        for path in paths:
            with read_jsonl(path) as records:
                for number, record in enumerate(records, 1):
                    message = record.get("message")
                    if isinstance(message, dict) and isinstance(message.get("content"), list):
                        record = {**record, "message": {**message, "content": [block for block in message["content"]
                                  if not isinstance(block, dict) or block.get("type") not in ("thinking", "redacted_thinking")]}}
                    yield HistoryEvent("claude", session["id"], str(record.get("uuid", f"{path.name}:{number}")),
                                       record.get("type", "unknown"), record, record.get("timestamp"),
                                       record.get("parentUuid"), {"file": str(path), "line": number,
                                       "subagent": path != main})


class CodexSource(RecordSource):
    """Читает rollout Codex из файлов или каталога; индекс SQLite не обязателен."""

    def paths(self) -> list[Path]:
        """Без входов возвращает rollout JSONL и Zstandard, предпочитая несжатые копии."""
        root = self.base / Path(self.options["path"]).expanduser()
        if root.is_file():
            return [root]
        if not root.is_dir():
            raise FileNotFoundError(root)
        paths = sorted([*root.rglob("rollout-*.jsonl"), *root.rglob("rollout-*.jsonl.zst")])
        return [path for path in paths if path.suffix != ".zst" or not path.with_suffix("").exists()]

    def list_sessions(self) -> list[dict]:
        """Без входов возвращает метаданные rollout, объединяя копии одной сессии."""
        sessions = {}
        for path in self.paths():
            with read_jsonl(path) as records:
                first = next(records, {})
            if first.get("type") != "session_meta" or not isinstance(first.get("payload"), dict):
                raise ValueError(f"Ожидался rollout Codex с session_meta: {path}")
            meta = first["payload"]
            if not meta.get("id") or not meta.get("cwd"):
                raise ValueError(f"В session_meta нет id/cwd: {path}")
            sessions.setdefault(str(meta["id"]), {"id": str(meta["id"]), "path": str(path),
                                "project": project_name(meta["cwd"]), "cwd": meta["cwd"]})
        return list(sessions.values())

    def iter_events(self, session: dict) -> Iterator[HistoryEvent]:
        """Принимает сессию; выдаёт реплики, инструменты, compaction и измерения без reasoning."""
        with read_jsonl(Path(session["path"])) as records:
            for number, record in enumerate(records, 1):
                payload = record.get("payload", {})
                kind = record.get("type", "unknown")
                if kind == "response_item" and payload.get("type") == "reasoning":
                    continue
                if kind == "event_msg" and payload.get("type") != "token_count":
                    continue
                yield HistoryEvent("codex", session["id"], str(number), kind, record,
                                   record.get("timestamp"), metadata={"file": session["path"], "line": number})


class GigaAgentSource(RecordSource):
    """Читает LangGraph history через API либо JSON-экспорт списка ThreadState."""

    def list_sessions(self) -> list[dict]:
        """Без входов возвращает явно выбранные thread_id либо записи экспорта threads."""
        if "path" in self.options:
            self.export = json.loads((self.base / self.options["path"]).read_text(encoding="utf-8-sig"))
            if not isinstance(self.export, dict) or not isinstance(self.export.get("threads"), list):
                raise ValueError("Экспорт GigaAgent должен содержать threads: [{thread_id, history}].")
            return [{"id": str(item["thread_id"])} for item in self.export["threads"]]
        identities = self.options.get("thread_ids")
        if not isinstance(identities, list) or not identities:
            raise ValueError("Для GigaAgent API укажи thread_ids.")
        return [{"id": str(identity)} for identity in identities]

    def history(self, session: dict) -> list[dict]:
        """Принимает сессию; возвращает все страницы checkpoints выбранного треда."""
        if "path" in self.options:
            return next(item["history"] for item in self.export["threads"] if str(item["thread_id"]) == session["id"])
        route = "threads/" + quote(session["id"], safe="") + "/history"
        history, before = [], None
        while True:
            body = {"limit": 100}
            if before:
                body["before"] = before
            page = request_json(self.options["url"], route, token_env=self.options.get("token_env"), body=body)
            if not isinstance(page, list):
                raise ValueError("LangGraph history должен возвращать список ThreadState.")
            if not page:
                break
            checkpoint = page[-1].get("checkpoint")
            if not checkpoint or checkpoint == before:
                raise ValueError("LangGraph history не продвигает пагинацию.")
            history.extend(page)
            before = checkpoint
        return history

    def iter_events(self, session: dict) -> Iterator[HistoryEvent]:
        """Принимает сессию; выдаёт сообщения только выбранной цепочки родительских checkpoints.

        Снимки повторно содержат контекст. Сообщение с тем же id и изменённым
        содержимым выдаётся как message_update; одинаковые тексты с разными id сохраняются.
        """
        history = self.history(session)
        if not history:
            return
        by_id = {item["checkpoint"]["checkpoint_id"]: item for item in history}
        head = self.options.get("checkpoint_id", history[0]["checkpoint"]["checkpoint_id"])
        chain, visited = [], set()
        while head:
            if head not in by_id or head in visited:
                raise ValueError("Неполная либо циклическая цепочка checkpoints GigaAgent.")
            visited.add(head)
            current = by_id[head]
            chain.append(current)
            head = (current.get("parent_checkpoint") or {}).get("checkpoint_id")
        seen = {}
        for checkpoint in reversed(chain):
            values = checkpoint.get("values", {})
            if not isinstance(values, dict) or not isinstance(values.get("messages", []), list):
                raise ValueError("В checkpoint ожидается values.messages.")
            checkpoint_id = checkpoint["checkpoint"]["checkpoint_id"]
            messages = values.get("messages", [])
            if any(not isinstance(message, dict) for message in messages):
                raise ValueError("Ожидалось сообщение LangChain в виде объекта.")
            if any(not message.get("id") for message in messages):
                yield HistoryEvent("gigaagent", session["id"], checkpoint_id, "checkpoint", checkpoint,
                                   checkpoint.get("created_at"), metadata={"input_messages_are_context": True,
                                   "message_identity_missing": True})
                continue
            for index, message in enumerate(messages):
                if not isinstance(message, dict):
                    raise ValueError("Ожидалось сообщение LangChain в виде объекта.")
                identity = str(message.get("id") or f"position:{index}")
                encoded = json.dumps(message, sort_keys=True, ensure_ascii=False)
                if seen.get(identity) == encoded:
                    continue
                kind = "message_update" if identity in seen else "message"
                seen[identity] = encoded
                yield HistoryEvent("gigaagent", session["id"], f"{checkpoint_id}:{identity}", kind,
                                   message, checkpoint.get("created_at"), metadata={"checkpoint_id": checkpoint_id,
                                   "message_id": identity, "checkpoint_metadata": checkpoint.get("metadata", {}),
                                   "tasks": checkpoint.get("tasks", [])})


class PhoenixSource(RecordSource):
    """Читает spans Phoenix из JSONL-экспорта либо постраничного REST API."""

    def records(self) -> Iterator[dict]:
        """Без входов выдаёт spans; курсор API проверяется на продвижение."""
        if "path" in self.options:
            yield from super().records()
            return
        cursor, visited = None, set()
        while True:
            parameters = {"limit": 100}
            if cursor:
                parameters["cursor"] = cursor
            route = "v1/projects/" + quote(self.options["project"], safe="") + "/spans?" + urlencode(parameters)
            page = request_json(self.options["url"], route, token_env=self.options.get("token_env"))
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise ValueError("Phoenix spans должен возвращать {data: [...], next_cursor: ...}.")
            yield from page["data"]
            cursor = page.get("next_cursor")
            if not cursor:
                break
            if cursor in visited:
                raise ValueError("Phoenix API повторил курсор.")
            visited.add(cursor)

    def session_id(self, record: dict) -> str:
        """Принимает span; возвращает session.id либо явно обозначенную отдельную trace."""
        session = attribute(record.get("attributes", {}), "session.id")
        trace = attribute(record, "context.trace_id")
        if session:
            return str(session)
        if not trace:
            raise ValueError("У span Phoenix нет session.id и context.trace_id.")
        return getattr(self, "trace_sessions", {}).get(str(trace), "trace:" + str(trace))

    def list_sessions(self) -> list[dict]:
        """Без входов возвращает сессии Phoenix и отдельные trace без session.id."""
        self.spans = list(self.records())
        trace_sessions = {}
        for span in self.spans:
            trace = attribute(span, "context.trace_id")
            identity = attribute(span.get("attributes", {}), "session.id")
            if trace and identity:
                trace_sessions.setdefault(str(trace), set()).add(str(identity))
        if any(len(identities) != 1 for identities in trace_sessions.values()):
            raise ValueError("В одной trace Phoenix обнаружены разные session.id.")
        self.trace_sessions = {trace: next(iter(identities)) for trace, identities in trace_sessions.items()}
        return [{"id": identity} for identity in dict.fromkeys(self.session_id(span) for span in self.spans)]

    def iter_events(self, session: dict) -> Iterator[HistoryEvent]:
        """Принимает сессию; выдаёт spans с деревом и пометкой входного контекста.

        Списки llm.input_messages остаются контекстом span, а не новыми репликами.
        Совпадение текста не удаляет разные span; конфликт копий одного span — ошибка.
        """
        seen = {}
        for span in sorted(self.spans, key=lambda item: item.get("start_time", "")):
            if self.session_id(span) != session["id"]:
                continue
            span_id = attribute(span, "context.span_id")
            trace_id = attribute(span, "context.trace_id")
            if not span_id or not trace_id:
                raise ValueError("У span нужны context.span_id и context.trace_id.")
            identity = str(trace_id) + ":" + str(span_id)
            if identity in seen:
                if seen[identity] != span:
                    raise ValueError(f"Конфликт копий span {identity}")
                continue
            seen[identity] = span
            yield HistoryEvent("phoenix", session["id"], identity, "span", span,
                               span.get("start_time"), span.get("parent_id"),
                               {"input_messages_are_context": True, "trace_id": trace_id})


_SOURCES = {"jsonl": RecordSource, "sqlite": RecordSource, "claude": ClaudeSource,
            "codex": CodexSource, "gigaagent": GigaAgentSource, "phoenix": PhoenixSource}


def register_history_source(name: str, factory) -> None:
    """Принимает уникальное имя и фабрику (параметры, каталог → источник); регистрирует её."""
    if not isinstance(name, str) or not name.strip() or name == "sql" or name in _SOURCES or not callable(factory):
        raise ValueError(f"Некорректная регистрация источника {name}.")
    _SOURCES[name] = factory


def load_history_source(options: dict, base: Path) -> HistorySource:
    """Принимает параметры и каталог конфигурации; возвращает зарегистрированный источник."""
    if not isinstance(options, dict) or options.get("type") not in (*_SOURCES, "sql"):
        raise ValueError("Неизвестный тип источника истории.")
    common = {"type", "session_ids", "projects", "limit"}
    fields = {
        "jsonl": {"path", "session_field", "user_field", "user_id"},
        "sqlite": {"path", "session_field", "user_field", "user_id", "query", "parameters"},
        "sql": {"driver", "dsn_env", "connect_kwargs", "query", "parameters",
                "session_field", "user_field", "user_id"},
        "claude": {"path", "include_subagents"}, "codex": {"path"},
        "gigaagent": {"path", "url", "token_env", "thread_ids", "checkpoint_id"},
        "phoenix": {"path", "url", "token_env", "project"},
    }
    kind = options["type"]
    if kind in fields:
        if set(options) - common - fields[kind]:
            raise ValueError(f"Неизвестные настройки источника {kind}: {set(options) - common - fields[kind]}")
        remote = kind in ("gigaagent", "phoenix")
        if (not remote and kind != "sql") or "path" in options:
            if not isinstance(options.get("path"), str) or not options["path"].strip():
                raise ValueError(f"Для {kind} нужен path.")
        if remote and ("path" in options) == ("url" in options):
            raise ValueError(f"Для {kind} выберите path либо url.")
        if remote and "url" in options and (not isinstance(options["url"], str) or not options["url"]):
            raise ValueError(f"Для {kind} нужен непустой url.")
        if kind == "phoenix" and "url" in options and not isinstance(options.get("project"), str):
            raise ValueError("Для Phoenix API нужен project.")
        if kind == "claude" and type(options.get("include_subagents", True)) is not bool:
            raise ValueError("include_subagents должен быть true либо false.")
    if kind == "sql":
        from command_agent_memory.history_sources.sql_history_source import SQLSource

        return SQLSource(options, base)
    return _SOURCES[kind](options, base)


def export_events(source: HistorySource, session: dict, output: Path) -> int:
    """Принимает источник, сессию и путь; пишет общий JSONL, возвращает число событий."""
    count = 0
    with output.open("w", encoding="utf-8") as stream:
        for event in source.iter_events(session):
            stream.write(json.dumps(remove_embedded_binary(asdict(event)), ensure_ascii=False) + "\n")
            count += 1
    if not count:
        raise ValueError(f"Сессия {session['id']} не содержит доступных событий.")
    return count
