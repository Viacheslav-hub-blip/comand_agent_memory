"""Версии командных знаний, их источники и состав действующей памяти.

Функции: load_registry читает реестр или сохраняет прежние документы как версии;
source_evidence создаёт проверяемые идентификаторы источников; registry_context
готовит читаемый контекст; apply_updates проверяет решения и сохраняет версии;
active_documents материализует только принятые знания; registry_snapshot
добавляет служебный реестр к публикуемому снимку. Модельных вызовов здесь нет.
"""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from command_agent_memory.memory_storage.memory_layout import MemoryLayout


REGISTRY_PATH = ".registry/state.json"


def load_registry(snapshot: dict[str, bytes]) -> dict:
    """Принимает снимок команды; возвращает реестр, проверяя соответствие активных файлов.

    Прежние документы без реестра сохраняются как принятые унаследованные версии.
    Их наличие не выдаётся за независимое подтверждение. INDEX.md строится заново.
    """
    if REGISTRY_PATH in snapshot:
        registry = json.loads(snapshot[REGISTRY_PATH])
        if (not isinstance(registry, dict) or set(registry) != {"version", "sources", "entries"}
                or registry["version"] != 1 or not isinstance(registry["sources"], dict)
                or not isinstance(registry["entries"], dict)):
            raise ValueError("Некорректный формат командного реестра.")
        expected = active_documents(registry)
        if expected != {name: data for name, data in snapshot.items() if name != REGISTRY_PATH}:
            raise RuntimeError("Действующая командная память не соответствует реестру.")
        return registry
    registry = {"version": 1, "sources": {}, "entries": {}}
    for name, data in sorted(snapshot.items()):
        if name == "INDEX.md":
            continue
        if name.startswith(".registry/"):
            raise ValueError("Неизвестный служебный файл командного реестра.")
        identifier = "legacy-" + hashlib.sha256(name.encode()).hexdigest()
        registry["entries"][identifier] = {"versions": [{
            "revision": 1, "run_id": "legacy", "status": "accepted", "statement": name,
            "conditions": "Условия сохранены в прежнем документе.",
            "reason": "Перенос существующей памяти; независимость оснований не установлена.",
            "evidence_ids": [], "conflicts_with": [],
            "documents": {name: base64.b64encode(data).decode("ascii")},
        }]}
    return registry


def source_evidence(sources: list[tuple[Path, dict, dict[str, bytes]]]) -> dict:
    """Принимает проверенные личные снимки; возвращает источники с устойчивыми ID.

    ID зависит от владельца, области, сессий, пути и содержимого документа,
    но не от повторного запуска или replay. Сессии хранятся отдельно: новая
    версия одного чата сама по себе не становится независимым подтверждением.
    """
    evidence = {}
    for run, metadata, files in sources:
        sessions = [json.loads(value) for value in sorted(
            json.dumps(value, sort_keys=True) for value in metadata.get("analysis_sessions", []))]
        for name, content in sorted(files.items()):
            source = {"user_id": metadata["user_id"],
                      "memory_identity": metadata.get("memory_store_identity", metadata["target"]),
                      "sessions": sessions, "document": name,
                      "sha256": hashlib.sha256(content).hexdigest()}
            identifier = "source-" + hashlib.sha256(json.dumps(
                source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            source["independence"] = "not_established"
            source["content_base64"] = base64.b64encode(content).decode("ascii")
            source["acceptances"] = [{"run": str(run), "replay_status": metadata["replay_status"],
                                      "replay_choice": metadata.get("replay_choice")}]
            if identifier in evidence:
                source["acceptances"] = evidence[identifier]["acceptances"] + source["acceptances"]
            evidence[identifier] = source
    return evidence


def registry_context(registry: dict) -> dict:
    """Принимает реестр; возвращает историю решений с читаемыми текущими документами.

    Бинарные приложения остаются в реестре без потерь, но не превращаются в текст.
    Старые версии представлены решениями и хешами, чтобы не дублировать весь архив.
    """
    context = deepcopy(registry)
    for source in context["sources"].values():
        source.pop("content_base64", None)
    for entry in context["entries"].values():
        for version in entry["versions"]:
            version["documents"] = {
                name: {"sha256": hashlib.sha256(base64.b64decode(data, validate=True)).hexdigest()}
                for name, data in version["documents"].items()
            }
    for identifier, entry in registry["entries"].items():
        documents = {}
        for name, data in entry["versions"][-1]["documents"].items():
            content = base64.b64decode(data, validate=True)
            try:
                documents[name] = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                documents[name] = "Бинарное приложение; исходные байты сохранены в реестре."
        context["entries"][identifier]["documents"] = documents
    return context


def apply_updates(registry: dict, updates: list[dict], evidence: dict, run_id: str) -> dict:
    """Принимает реестр, решения LLM, проверенные источники и ID запуска; возвращает новую версию.

    Проверяет ссылки на источники, симметрию конфликтов и новое основание при
    принятии ранее спорного знания. Счётчик голосов и числовой порог не вводятся.
    Повтор одинакового решения не создаёт новую версию знания.
    """
    if not isinstance(updates, list):
        raise ValueError("updates должен быть списком решений по знаниям.")
    result = deepcopy(registry)
    for identifier, source in evidence.items():
        previous = result["sources"].get(identifier)
        merged = deepcopy(source)
        if previous:
            merged["acceptances"] = previous["acceptances"] + [
                item for item in source["acceptances"] if item not in previous["acceptances"]]
        result["sources"][identifier] = merged
    seen = set()
    fields = {"id", "status", "statement", "conditions", "reason", "evidence_ids", "conflicts_with", "documents"}
    for update in updates:
        if not isinstance(update, dict) or set(update) != fields:
            raise ValueError("Некорректная схема решения командного реестра.")
        identifier = update["id"]
        if (not isinstance(identifier, str) or not identifier or len(identifier) > 128
                or any(not (character.isascii() and (character.isalnum() or character in "_-"))
                       for character in identifier) or identifier in seen):
            raise ValueError("Нужен уникальный ID знания из латинских букв, цифр, _ и -.")
        seen.add(identifier)
        if update["status"] not in ("accepted", "pending", "conflicts"):
            raise ValueError("Неизвестный статус знания.")
        if any(not isinstance(update[key], str) or not update[key].strip()
               for key in ("statement", "conditions", "reason")):
            raise ValueError("Укажите утверждение, условия и основание решения.")
        for key in ("evidence_ids", "conflicts_with"):
            values = update[key]
            if (not isinstance(values, list) or any(not isinstance(value, str) for value in values)
                    or len(set(values)) != len(values)):
                raise ValueError(f"{key} должен содержать уникальные строковые ID.")
        if not update["evidence_ids"] or set(update["evidence_ids"]) - result["sources"].keys():
            raise ValueError("Знание должно ссылаться на существующие проверенные источники.")
        documents = update["documents"]
        if not isinstance(documents, dict) or not documents:
            raise ValueError("documents должен содержать документы знания.")
        encoded = {}
        for name, content in documents.items():
            if not isinstance(name, str):
                raise ValueError("Имя документа должно быть строкой.")
            if isinstance(content, str) and content.strip():
                encoded[name] = base64.b64encode(content.encode("utf-8")).decode("ascii")
            elif (isinstance(content, dict) and set(content) == {"source_id"}
                  and isinstance(content["source_id"], str) and content["source_id"] in update["evidence_ids"]):
                encoded[name] = result["sources"][content["source_id"]]["content_base64"]
            else:
                raise ValueError("Документ должен содержать непустой текст или ссылку source_id на своё основание.")
        previous = result["entries"].get(identifier, {}).get("versions", [])
        if (previous and previous[-1]["status"] != "accepted" and update["status"] == "accepted"
                and not set(update["evidence_ids"]) - set(previous[-1]["evidence_ids"])):
            raise ValueError("Для принятия pending/conflicts нужно новое основание из источников.")
        version = {key: deepcopy(value) for key, value in update.items() if key != "id"}
        version["evidence_ids"] = sorted(version["evidence_ids"])
        version["conflicts_with"] = sorted(version["conflicts_with"])
        version["documents"] = encoded
        if previous:
            for name, data in previous[-1]["documents"].items():
                try:
                    base64.b64decode(data, validate=True).decode("utf-8-sig")
                except UnicodeDecodeError:
                    if name in documents and isinstance(documents[name], str):
                        raise ValueError("Бинарное приложение нельзя заменить текстом решения LLM.")
                    version["documents"].setdefault(name, data)
        if previous and all(previous[-1][key] == value for key, value in version.items()):
            continue
        version.update(revision=len(previous) + 1, run_id=run_id)
        result["entries"][identifier] = {"versions": [*previous, version]}
    active_documents(result)
    return result


def active_documents(registry: dict) -> dict[str, bytes]:
    """Принимает реестр; возвращает только accepted-документы и индекс, проверяя пути и конфликты.

    Один активный файл принадлежит одному знанию: спорную часть следует выделить
    в отдельную запись. Pending/conflicts и их архивные версии агенту не передаются.
    """
    documents = {}
    for source in registry["sources"].values():
        if "content_base64" in source:
            content = base64.b64decode(source["content_base64"], validate=True)
            if hashlib.sha256(content).hexdigest() != source["sha256"]:
                raise ValueError("Содержимое источника в реестре не соответствует SHA-256.")
    current = {}
    for identifier, entry in registry["entries"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("versions"), list) or not entry["versions"]:
            raise ValueError("Запись реестра должна содержать непустую историю версий.")
        current[identifier] = entry["versions"][-1]
    for identifier, version in current.items():
        if version["status"] not in ("accepted", "pending", "conflicts"):
            raise ValueError("Неизвестный статус в командном реестре.")
        conflicts = version["conflicts_with"]
        if set(version["evidence_ids"]) - registry["sources"].keys():
            raise ValueError("В реестре указаны неизвестные источники знания.")
        if (version["status"] == "conflicts") != bool(conflicts):
            raise ValueError("Только conflicts должен содержать ссылки на несовместимые знания.")
        for other in conflicts:
            if (other == identifier or other not in current or current[other]["status"] != "conflicts"
                    or identifier not in current[other]["conflicts_with"]):
                raise ValueError("Ссылки conflicts должны быть взаимными и вести на спорные знания.")
        for name, data in version["documents"].items():
            MemoryLayout((name,))
            if Path(name).as_posix() != name:
                raise ValueError("Путь документа должен быть каноническим относительным POSIX-путём.")
            if name.casefold() == "index.md" or Path(name).parts[0].casefold() == ".registry":
                raise ValueError("INDEX.md и .registry управляются приложением.")
            content = base64.b64decode(data, validate=True)
            if version["status"] == "accepted":
                if name in documents:
                    raise ValueError(f"Документ принадлежит нескольким активным знаниям: {name}")
                documents[name] = content
    if documents:
        MemoryLayout(tuple(documents))
    for version in current.values():
        if version["status"] == "conflicts" and any(name in documents for name in version["documents"]):
            raise ValueError("Спорный документ не может одновременно входить в действующую память.")
    index = "# Командная память\n\n" + "\n".join(
        f"- [{name.replace('[', '').replace(']', '')}]({quote(name, safe='/')})" for name in sorted(documents))
    documents["INDEX.md"] = (index + "\n").encode("utf-8")
    return documents


def registry_snapshot(registry: dict) -> dict[str, bytes]:
    """Принимает реестр; возвращает действующие документы и служебный JSON для общей публикации."""
    return {**active_documents(registry), REGISTRY_PATH: json.dumps(
        registry, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")}
