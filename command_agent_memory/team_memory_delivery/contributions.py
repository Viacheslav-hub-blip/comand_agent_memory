"""Пакеты принятых знаний и проверки перед переносом между устройствами.

Функции: json_bytes сериализует JSON; digest возвращает SHA-256; document_path
проверяет переносимый путь. check_json_credentials проверяет пары JSON,
screen_text проверяет UTF-8 и признаки секретов.
build_contribution формирует пакет; validate_contribution проверяет пакет;
write_atomic сохраняет служебные файлы; save_pending и load_pending
работают с локальной очередью.
"""
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from tempfile import NamedTemporaryFile
import unicodedata

from command_agent_memory.memory_storage.accepted_knowledge import read_accepted_knowledge


CONTRIBUTION_ID = re.compile(r"[0-9a-f]{64}\Z")
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
CONTRIBUTION_FIELDS = ("version", "team_id", "user_id", "sessions", "documents")
JSON_CREDENTIAL_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?(?:key|token)|access[_-]?token|refresh[_-]?token"
    r"|password|secret|client[_-]?secret|private[_-]?key|token)\Z", re.IGNORECASE,
)
SECRET = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
    r"|(?<![\w-])(?:sk-or-v1-|sk-proj-|ghp_|github_pat_|ATATT3x)[a-zA-Z0-9_-]{12,}"
    r"|(?im:\b(?:api[_-]?key|access[_-]?token|password|secret)[\x22\x27]?\s*[:=]\s*['\x22]?[a-z0-9_./+-]{12,})"
    r"|(?i:\bhttps?://[^/\s@]+:[^/\s@]+@)"
    r"|(?im:\b[A-Z]:[\\/][^\r\n]*|(?:^|[\s\x60(\x22'])/(?:home|Users|tmp|var|mnt)/[^\s]+)"
)
DOCUMENT_SUFFIXES = {".md", ".txt", ".sql", ".csv", ".json", ".yml", ".yaml", ".toml"}
WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))
}


def json_bytes(value: object) -> bytes:
    """Принимает объект JSON; возвращает стабильное представление UTF-8."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(data: bytes) -> str:
    """Принимает байты; возвращает их SHA-256 в нижнем регистре."""
    return hashlib.sha256(data).hexdigest()


def document_path(name: str, *, prefix: bool = False) -> PurePosixPath:
    """Принимает имя документа или область экспорта; возвращает переносимый POSIX-путь."""
    if not isinstance(name, str) or not name or name != unicodedata.normalize("NFC", name):
        raise ValueError("Путь командной памяти должен быть непустым и нормализованным.")
    path = PurePosixPath(name)
    if (path.as_posix() != name or path.is_absolute() or not path.parts
            or PureWindowsPath(name).drive or any(char in name for char in '\\:*?"<>|')
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or any(part.startswith(".") or part.endswith((" ", ".")) or part.split(".")[0].casefold() in WINDOWS_RESERVED
                   for part in path.parts)):
        raise ValueError("В командной памяти обнаружен непереносимый путь.")
    if any(part.casefold() in {"agents.md", "user_preferences.md", "user_llm_wiki",
                               "local_settings", "credentials"} for part in path.parts):
        raise ValueError("Путь содержит личные или служебные файлы.")
    if not prefix and path.suffix.lower() not in DOCUMENT_SUFFIXES:
        raise ValueError("Командный пакет принимает только текстовые документы поддерживаемых форматов.")
    return path


def check_json_credentials(pairs: list[tuple[str, object]]) -> dict:
    """Принимает пары полей JSON. Возвращает объект после проверки значений ключей."""
    for key, value in pairs:
        if JSON_CREDENTIAL_KEY.search(key) and value not in (None, "", [], {}):
            raise ValueError("В JSON командного пакета заполнено поле учётных данных.")
    return dict(pairs)


def screen_text(data: bytes) -> None:
    """Принимает байты документа; при признаках секрета или локального пути поднимает ошибку."""
    try:
        value = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Командный пакет должен содержать текстовые документы UTF-8.") from None
    if "\0" in value or SECRET.search(value):
        raise ValueError("В командных материалах найден секрет или локальный путь.")
    if value.lstrip().startswith(("{", "[")):
        try:
            json.loads(value, object_pairs_hook=check_json_credentials)
        except json.JSONDecodeError:
            # Фрагмент JSON в тексте уже проверен общим фильтром.
            pass


def build_contribution(team_id: str, export_paths: tuple[PurePosixPath, ...], run: Path
                       ) -> tuple[dict, dict[str, bytes]] | None:
    """Принимает команду, разрешённые пути и принятый журнал; возвращает манифест с документами."""
    metadata, accepted = read_accepted_knowledge(run)
    documents = {}
    for name, data in accepted.items():
        pure = PurePosixPath(name)
        if not any(pure.is_relative_to(prefix) for prefix in export_paths):
            continue
        document_path(name)
        portable_name = name.removeprefix("knowledge/")
        if portable_name in documents:
            raise ValueError("После переноса путей командных документов образовалась коллизия.")
        documents[portable_name] = data
    if not documents:
        return None
    sessions = []
    for session in metadata.get("analysis_sessions", []):
        sessions.append(digest(json_bytes({"user": metadata["user_id"],
                                           "source": session.get("source"),
                                           "session": session.get("session_id")})))
    descriptor = {
        "version": 1,
        "team_id": team_id,
        "user_id": metadata["user_id"],
        "sessions": sorted(set(sessions)),
        "documents": {name: digest(data) for name, data in documents.items()},
    }
    # Повторный анализ и replay одного снимка не создают нового вклада.
    manifest = {**descriptor, "id": digest(json_bytes(descriptor)), "personal_run": run.parent.name,
                "replay_status": metadata["replay_status"], "replay_choice": metadata.get("replay_choice")}
    validate_contribution(team_id, manifest["id"], manifest, documents)
    return manifest, documents


def validate_contribution(team_id: str, identifier: str, manifest: dict, documents: dict[str, bytes]) -> None:
    """Принимает пакет и ожидаемый ID; проверяет версию, происхождение, пути и хеши."""
    keys = {"version", "team_id", "user_id", "sessions", "documents",
            "id", "personal_run", "replay_status", "replay_choice"}
    if not isinstance(manifest, dict) or set(manifest) != keys or manifest["version"] != 1:
        raise ValueError("Неизвестный формат манифеста командного вклада.")
    owner = manifest["user_id"]
    if (not CONTRIBUTION_ID.fullmatch(identifier) or manifest["id"] != identifier
            or manifest["team_id"] != team_id or not isinstance(owner, str) or not owner
            or not all(char.isalnum() or char in "_-" for char in owner)
            or not isinstance(manifest["personal_run"], str) or not RUN_ID.fullmatch(manifest["personal_run"])):
        raise ValueError("Некорректные идентификаторы командного вклада.")
    replay = manifest["replay_status"]
    if (replay not in ("not_requested", "skipped", "completed")
            or manifest["replay_choice"] != ("candidate" if replay == "completed" else None)):
        raise ValueError("Командный вклад должен иметь основание принятия.")
    sessions = manifest["sessions"]
    if (not isinstance(sessions, list) or any(not isinstance(item, str) or not CONTRIBUTION_ID.fullmatch(item)
                                              for item in sessions) or sessions != sorted(set(sessions))):
        raise ValueError("Некорректное происхождение командного вклада.")
    if not documents or manifest["documents"] != {name: digest(data) for name, data in documents.items()}:
        raise ValueError("Хеши документов командного вклада не совпадают.")
    portable = set()
    for name, data in documents.items():
        document_path(name)
        screen_text(data)
        if name.casefold() in portable:
            raise ValueError("Командный вклад содержит конфликт имён файлов.")
        portable.add(name.casefold())
    descriptor = {name: manifest[name] for name in CONTRIBUTION_FIELDS}
    if identifier != digest(json_bytes(descriptor)):
        raise ValueError("Идентификатор командного вклада не соответствует содержимому.")


def write_atomic(path: Path, data: bytes) -> None:
    """Принимает путь и байты; устойчиво заменяет файл тем же файловым томом."""
    with NamedTemporaryFile(dir=path.parent, prefix=".write-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_pending(queue: Path, manifest: dict, documents: dict[str, bytes]) -> None:
    """Принимает очередь и пакет; сохраняет его атомарно до отправки, возвращает None."""
    queue.mkdir(parents=True, exist_ok=True)
    path = queue / f"{manifest['id']}.json"
    if path.exists():
        return
    value = {"manifest": manifest,
             "documents": {name: base64.b64encode(data).decode("ascii") for name, data in documents.items()}}
    write_atomic(path, json_bytes(value))


def load_pending(path: Path, team_id: str) -> tuple[dict, dict[str, bytes]]:
    """Принимает файл очереди; возвращает проверенный манифест и исходные документы."""
    value = json.loads(path.read_bytes())
    if (not isinstance(value, dict) or set(value) != {"manifest", "documents"}
            or not isinstance(value["documents"], dict)
            or any(not isinstance(data, str) for data in value["documents"].values())):
        raise ValueError("Файл очереди командных вкладов повреждён.")
    manifest = value["manifest"]
    files = {name: base64.b64decode(data, validate=True) for name, data in value["documents"].items()}
    validate_contribution(team_id, path.stem, manifest, files)
    return manifest, files
