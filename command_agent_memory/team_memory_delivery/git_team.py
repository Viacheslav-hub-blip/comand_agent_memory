"""Отправка, консолидация и получение командной памяти через Git.

Класс GitTeamConfig проверяет настройки; методы from_json и main_ref читают
конфигурацию и возвращают имя ветки. Функции: canonical_snapshot читает
каноническую ветку; read_contribution проверяет пакет; submit_contributions
отправляет очередь; process_contribution и process_contributions применяют
вклады. sync_git_team материализует и доставляет снимок, load_version_marker
читает служебный файл, read_team_version
закрепляет доставленную версию; contribution_metadata возвращает происхождение вклада;
open_repository привязывает кэш; member_status читает состояние вкладов отправителя;
run_git_team выбирает операцию.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import re
import shutil
from tempfile import TemporaryDirectory
from typing import Callable
from urllib.parse import urlsplit

from command_agent_memory.application_config import resolve_path
from command_agent_memory.memory_storage.file_storage import FileMemoryStore, checked_path
from command_agent_memory.memory_storage.memory_locking import memory_lock
from command_agent_memory.memory_storage.memory_snapshots import write_snapshot
from command_agent_memory.memory_storage.team_registry import REGISTRY_PATH, active_documents, load_registry
from command_agent_memory.run_reports import create_run_directory
from command_agent_memory.team_memory_delivery.contributions import (
    CONTRIBUTION_FIELDS, CONTRIBUTION_ID, build_contribution, digest, document_path, json_bytes,
    load_pending, save_pending, screen_text, validate_contribution, write_atomic,
)
from command_agent_memory.team_memory_delivery.git_repository import GitCommandError, GitRepository, OBJECT_ID


BRANCH_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_/-]*\Z")
TEAM_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
CONTRIBUTIONS = "refs/heads/contributions/"
TeamProposer = Callable[[dict[str, bytes], tuple[Path, dict, dict[str, bytes]], Path], dict[str, bytes] | None]
VERSION_FILE = "team_knowledge/.team-version.json"


@dataclass(frozen=True)
class GitTeamConfig:
    """Хранит проверенный адрес, ветку, область экспорта и путь локального кэша."""

    team_id: str
    repository: str
    branch: str
    cache_dir: Path
    export_paths: tuple[PurePosixPath, ...]

    @property
    def main_ref(self) -> str:
        """Без входов возвращает полное имя канонической ветки в Git."""
        return "refs/heads/" + self.branch

    @classmethod
    def from_json(cls, team: dict, base: Path, users: list[dict]) -> "GitTeamConfig":
        """Принимает JSON, базовый путь и участников; возвращает настройки без обращения к сети."""
        fields = {"type", "team_id", "repository", "cache_dir", "export_paths", "branch", "workspaces"}
        if set(team) - fields:
            raise ValueError("В team.type=git указаны неизвестные поля.")
        team_id, repository, branch = team.get("team_id"), team.get("repository"), team.get("branch", "main")
        if not isinstance(team_id, str) or not TEAM_ID.fullmatch(team_id):
            raise ValueError("Для Git-команды нужен team_id из латинских букв, цифр, _ или -.")
        if (not isinstance(branch, str) or not BRANCH_NAME.fullmatch(branch) or "//" in branch
                or branch.endswith("/") or branch == "contributions" or branch.startswith("contributions/")):
            raise ValueError("Укажите допустимую каноническую ветку Git.")
        if (not isinstance(repository, str) or not repository or repository.startswith("-")
                or any(ord(char) < 32 for char in repository)):
            raise ValueError("Для Git-команды нужен адрес repository.")
        if "://" in repository:
            url = urlsplit(repository)
            if (url.scheme not in ("https", "ssh") or not url.hostname or url.password is not None
                    or (url.scheme == "https" and url.username is not None) or url.query or url.fragment):
                raise ValueError("Нужен чистый HTTPS или SSH-адрес Git без секретов.")
        elif not re.fullmatch(r"(?:[\w.-]+@)?[\w.-]{2,}:[^:\s]+", repository):
            if ":" in repository and not re.match(r"^[a-zA-Z]:[/\\]", repository):
                raise ValueError("Неподдерживаемый адрес Git.")
            repository = str(resolve_path(base, repository).resolve())
        cache = resolve_path(base, team.get("cache_dir"))
        if cache == Path(cache.anchor) or base.resolve().is_relative_to(cache.resolve()):
            raise ValueError("Кэш Git должен быть отдельным рабочим каталогом.")
        checked_path(cache.parent, cache.name)
        protected = [path for user in users for path in (user["workspace"], *user["store"].protected_paths)]
        for user in users:
            source = user["source_options"].get("path")
            if isinstance(source, str):
                protected.append(resolve_path(base, source))
        if Path(repository).is_absolute():
            protected.append(Path(repository))
        if any(cache.resolve().is_relative_to(path.resolve()) or path.resolve().is_relative_to(cache.resolve())
               for path in protected):
            raise ValueError("Кэш Git пересекается с историей, памятью, журналом или репозиторием.")
        exports = team.get("export_paths")
        if not isinstance(exports, list) or (users and not exports):
            raise ValueError("Задайте export_paths со списком разрешённых путей знаний.")
        export_paths = tuple(document_path(name, prefix=True) for name in exports)
        if not shutil.which("git"):
            raise ValueError("Для командной памяти требуется Git в PATH.")
        return cls(team_id, repository, branch, cache, export_paths)


def canonical_snapshot(files: dict[str, bytes]) -> tuple[dict[str, bytes], dict[str, dict]]:
    """Принимает дерево Git; возвращает проверенную память команды и квитанции вкладов."""
    knowledge, receipts = {}, {}
    for name, content in files.items():
        if name.startswith("knowledge/"):
            key = name.removeprefix("knowledge/")
            if key != REGISTRY_PATH:
                document_path(key)
            screen_text(content)
            knowledge[key] = content
        elif name.startswith("receipts/") and name.endswith(".json"):
            identifier = name.removeprefix("receipts/").removesuffix(".json")
            receipt = json.loads(content)
            if (not CONTRIBUTION_ID.fullmatch(identifier) or not isinstance(receipt, dict)
                    or receipt.get("id") != identifier
                    or receipt.get("state") not in ("applied", "unchanged", "needs_resolution")
                    or not isinstance(receipt.get("package_sha256"), str)
                    or not CONTRIBUTION_ID.fullmatch(receipt["package_sha256"])
                    or not isinstance(receipt.get("base_commit"), str)
                    or not OBJECT_ID.fullmatch(receipt["base_commit"])):
                raise ValueError("Некорректная квитанция командного вклада.")
            receipts[identifier] = receipt
        else:
            raise ValueError("Каноническая ветка должна содержать только knowledge/ и receipts/.")
    if knowledge:
        load_registry(knowledge)
        if "INDEX.md" not in knowledge:
            raise ValueError("Опубликованная память должна содержать INDEX.md.")
    return knowledge, receipts


def read_contribution(repo: GitRepository, config: GitTeamConfig, identifier: str,
                      commit: str) -> tuple[dict, dict[str, bytes]]:
    """Принимает commit вклада; возвращает проверенные манифест и документы."""
    if not CONTRIBUTION_ID.fullmatch(identifier):
        raise ValueError("Имя ветки вклада должно заканчиваться идентификатором SHA-256.")
    files = repo.tree(commit)
    root = f"contributions/{identifier}/"
    manifest_bytes = files.pop(root + "manifest.json", None)
    if manifest_bytes is None:
        raise ValueError("В пакете нет манифеста.")
    prefix = root + "knowledge/"
    documents = {name.removeprefix(prefix): data for name, data in files.items() if name.startswith(prefix)}
    parent_files = repo.tree(repo.parent(commit))
    canonical_snapshot(parent_files)
    if {name: data for name, data in files.items() if not name.startswith(prefix)} != parent_files:
        raise ValueError("Ветка вклада меняет файлы вне своего пакета.")
    manifest = json.loads(manifest_bytes)
    validate_contribution(config.team_id, identifier, manifest, documents)
    return manifest, documents


def submit_contributions(repo: GitRepository, config: GitTeamConfig, accepted: list[Path],
                         result: dict) -> None:
    """Принимает принятые журналы; сохраняет вклады в очереди и отправляет до подтверждения."""
    queue = checked_path(config.cache_dir, "outbox")
    failures = []
    result.update(state="skipped", contributions=[])
    for run in accepted:
        try:
            package = build_contribution(config.team_id, config.export_paths, run)
            if package is not None:
                save_pending(queue, *package)
        except Exception as error:
            failures.append(error)
            result["contributions"].append({"run": str(run), "state": "failed", "error": str(error)})
    pending = [path for path in sorted(queue.glob("*.json")) if not path.with_suffix(".sent").exists()]
    if pending:
        try:
            base = repo.fetch(config.main_ref)
        except GitCommandError:
            result["contributions"].extend({"id": path.stem, "state": "queued"} for path in pending)
            raise
        canonical = repo.tree(base)
        canonical_snapshot(canonical)
        for path in pending:
            report = {"id": path.stem, "state": "queued"}
            result["contributions"].append(report)
            try:
                manifest, documents = load_pending(path, config.team_id)
                identifier = manifest["id"]
                ref = CONTRIBUTIONS + identifier
                root = f"contributions/{identifier}/"
                if not repo.refs(ref):
                    files = {**canonical, root + "manifest.json": json_bytes(manifest)}
                    files.update({root + "knowledge/" + name: data for name, data in documents.items()})
                    commit = repo.commit(base, files, f"Contribution {identifier}")
                    try:
                        repo.push(commit, ref)
                    except GitCommandError:
                        if not repo.refs(ref):
                            raise
                published = repo.fetch(ref)
                remote_manifest, remote_documents = read_contribution(repo, config, identifier, published)
                if (remote_documents != documents
                        or any(remote_manifest[key] != manifest[key] for key in CONTRIBUTION_FIELDS)):
                    raise ValueError("Удалённый вклад с таким ID содержит другой пакет.")
                path.with_suffix(".sent").touch()
                report.update(state="submitted", commit=published)
            except (GitCommandError, ValueError, OSError) as error:
                failures.append(error)
                report["error"] = str(error)
        result["state"] = "submitted"
    elif accepted and not failures:
        result["state"] = "unchanged"
    if failures:
        raise failures[0]


def contribution_metadata(config: GitTeamConfig, identifier: str, manifest: dict) -> dict:
    """Принимает конфигурацию и манифест вклада; возвращает воспроизводимое происхождение для реестра."""
    return {
        "contribution_id": identifier, "memory_layout": {},
        "user_id": manifest["user_id"], "replay_status": manifest["replay_status"],
        "replay_choice": manifest["replay_choice"],
        "memory_store_identity": f"git:{config.team_id}:{manifest['user_id']}",
        "analysis_sessions": [{"session_id": session} for session in manifest["sessions"]],
    }


def process_contribution(repo: GitRepository, config: GitTeamConfig, identifier: str,
                         manifest: dict, files: dict[str, bytes], propose: TeamProposer) -> dict:
    """Принимает проверенный вклад; применяет его к текущему main и возвращает квитанцию."""
    package_sha256 = digest(json_bytes(manifest))
    base = repo.fetch(config.main_ref)
    for _ in range(3):
        canonical = repo.tree(base)
        before, receipts = canonical_snapshot(canonical)
        previous = receipts.get(identifier)
        if previous is not None:
            if previous["package_sha256"] != package_sha256:
                raise ValueError("Вклад с таким ID уже обработан с другим хешем пакета.")
            return {**previous, "reused": True}
        run = create_run_directory(config.cache_dir / "team-runs")
        source = run / "contribution"
        write_snapshot(source / "after", files)
        write_snapshot(run / "before", before)
        metadata = contribution_metadata(config, identifier, manifest)
        after = propose(before, (source, metadata, files), run)
        if after is None:
            after, state = before, "needs_resolution"
        else:
            write_snapshot(run / "after", after)
            state = "applied" if after != before else "unchanged"
        receipt = {"id": identifier, "package_sha256": package_sha256, "base_commit": base, "state": state}
        decision_path = run / "decision.json"
        if state == "needs_resolution" and decision_path.is_file():
            decision = json.loads(decision_path.read_bytes())
            note = "\n".join([decision["summary"], *(item["change"] for item in decision["findings"])])
            try:
                screen_text(note.encode())
            except ValueError:
                note = "Уточнение доступно в локальном журнале командного исполнителя."
            receipt["resolution"] = note
        proposed = {name: data for name, data in canonical.items() if name.startswith("receipts/")}
        proposed.update({"knowledge/" + name: data for name, data in after.items()})
        proposed[f"receipts/{identifier}.json"] = json_bytes(receipt)
        canonical_snapshot(proposed)
        commit = repo.commit(base, proposed, f"Process contribution {identifier}")
        try:
            repo.push(commit, config.main_ref)
        except GitCommandError:
            latest = repo.fetch(config.main_ref)
            _, current_receipts = canonical_snapshot(repo.tree(latest))
            current = current_receipts.get(identifier)
            if current is not None:
                if current["package_sha256"] != package_sha256:
                    raise ValueError("Удалённая квитанция указывает на другой пакет.")
                return {**current, "commit": latest, "run": str(run), "reused": True}
            if latest == base:
                raise
            base = latest
            continue
        return {**receipt, "commit": commit, "run": str(run)}
    raise RuntimeError("Каноническая ветка изменилась трижды. Вклад останется для следующего запуска.")


def process_contributions(repo: GitRepository, config: GitTeamConfig, propose: TeamProposer,
                          result: dict) -> None:
    """Принимает Git-команду и обработчик; обрабатывает опубликованные пакеты независимо."""
    result.update(state="unchanged", contributions=[])
    failures = []
    for ref, expected in sorted(repo.refs(CONTRIBUTIONS + "*").items()):
        identifier = ref.removeprefix(CONTRIBUTIONS)
        try:
            actual = repo.fetch(ref)
            if expected != actual:
                raise RuntimeError("Ветка вклада изменилась во время чтения.")
            manifest, files = read_contribution(repo, config, identifier, actual)
            receipt = process_contribution(repo, config, identifier, manifest, files, propose)
            result["contributions"].append(receipt)
            if not receipt.get("reused"):
                result["state"] = "processed"
        except Exception as error:
            failures.append(error)
            result["contributions"].append({"id": identifier, "state": "failed", "error": str(error)})
    if failures:
        raise failures[0]


def sync_git_team(repo: GitRepository, config: GitTeamConfig, workspaces: list[Path], result: dict) -> None:
    """Принимает Git-команду и рабочие каталоги; доставляет проверенный снимок и сообщает его SHA."""
    pointer = checked_path(config.cache_dir, "last-sync.json")
    try:
        commit = repo.fetch(config.main_ref)
        version = {"team_id": config.team_id, "commit": commit,
                   "synchronized_at": datetime.now(timezone.utc).isoformat()}
        state = "synced"
    except GitCommandError as error:
        if not pointer.is_file():
            raise GitCommandError(f"Кэш командной памяти ещё не создан. {error}") from None
        result["remote_error"] = str(error)
        version = json.loads(pointer.read_bytes())
        commit = version["commit"]
        state = "cached"
    knowledge, _ = canonical_snapshot(repo.tree(commit))
    if not knowledge:
        result.update(state="empty", commit=commit)
        return
    active = active_documents(load_registry(knowledge)) if REGISTRY_PATH in knowledge else knowledge
    version_path = config.cache_dir / "snapshots" / commit
    checked_path(config.cache_dir, "snapshots/" + commit)
    if not version_path.exists():
        version_path.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=version_path.parent) as temp:
            snapshot = Path(temp) / "snapshot"
            write_snapshot(snapshot, active)
            snapshot.rename(version_path)
    if FileMemoryStore(version_path, (), whole_directory=True).snapshot(recover=False) != active:
        raise ValueError("Файлы в кэше командного снимка изменились.")
    if state == "synced":
        repo.run("update-ref", "refs/command-agent-memory/last-sync", commit)
        write_atomic(pointer, json_bytes(version))
    result.update(state=state, commit=commit, snapshot=str(version_path), deliveries=[])
    delivered_version = {**version, "document_sha256": {name: digest(data) for name, data in active.items()}}
    for workspace in workspaces:
        store = FileMemoryStore(workspace, ("team_knowledge",))
        with memory_lock(workspace / "team_knowledge"):
            before = store.snapshot()
            after = {"team_knowledge/" + name: data for name, data in active.items()}
            previous = load_version_marker(before.get(VERSION_FILE))
            same_version = all(previous.get(key) == delivered_version[key]
                               for key in ("commit", "team_id", "document_sha256"))
            after[VERSION_FILE] = before[VERSION_FILE] if same_version else json_bytes(delivered_version)
            if before != after:
                run = create_run_directory(config.cache_dir / "delivery")
                store.publish(before, after, run)
                (run / "status.json").write_bytes(json_bytes({"commit": commit, "state": "published",
                                                            "target": str(workspace / "team_knowledge")}))
            result["deliveries"].append({"workspace": str(workspace), "commit": commit,
                                         "state": "published" if before != after else "unchanged"})


def load_version_marker(data: bytes | None) -> dict:
    """Принимает байты метки Git. Возвращает объект или пустой словарь при ошибке формата."""
    if data is None:
        return {}
    try:
        marker = json.loads(data)
    except ValueError:
        return {}
    return marker if isinstance(marker, dict) else {}


@contextmanager
def read_team_version(workspace: Path):
    """Принимает рабочий каталог. Проверяет документы и удерживает снимок, выдавая SHA или None."""
    store = FileMemoryStore(workspace, ("team_knowledge",))
    with store.read_session(verify_unchanged=True) as snapshot:
        marker = snapshot.get(VERSION_FILE)
        if marker is None:
            yield None
            return
        version = load_version_marker(marker)
        commit = version.get("commit")
        if not isinstance(commit, str) or not OBJECT_ID.fullmatch(commit):
            raise ValueError("Командная память содержит некорректный SHA. Выполните --team-sync.")
        documents = {name.removeprefix("team_knowledge/"): digest(data)
                     for name, data in snapshot.items() if name != VERSION_FILE}
        if version.get("document_sha256") != documents or "INDEX.md" not in documents:
            raise ValueError("Файлы командной памяти не совпадают с доставленной версией. Выполните --team-sync.")
        yield commit


def open_repository(config: GitTeamConfig) -> GitRepository:
    """Принимает настройки под блокировкой кэша; проверяет привязку и возвращает Git-репозиторий."""
    config.cache_dir.mkdir(parents=True, exist_ok=True)
    identity = checked_path(config.cache_dir, "team.json")
    binding = json_bytes({"repository": config.repository, "team_id": config.team_id, "branch": config.branch})
    if identity.exists() and identity.read_bytes() != binding:
        raise ValueError("Кэш Git уже связан с другим репозиторием или веткой.")
    if not identity.exists():
        write_atomic(identity, binding)
    objects = checked_path(config.cache_dir, "objects.git")
    return GitRepository(objects, config.repository)


def member_status(repo: GitRepository, config: GitTeamConfig) -> dict:
    """Принимает Git и настройки отправителя; возвращает очередь, квитанции и текущее состояние связанных знаний."""
    outbox = checked_path(config.cache_dir, "outbox")
    commit = repo.fetch(config.main_ref)
    knowledge, receipts = canonical_snapshot(repo.tree(commit))
    registry = load_registry(knowledge)
    refs = repo.refs(CONTRIBUTIONS + "*")
    contributions = []
    for path in sorted(outbox.glob("*.json")):
        identifier = path.stem
        manifest, _ = load_pending(path, config.team_id)
        receipt = receipts.get(identifier)
        state = "awaiting_processing" if CONTRIBUTIONS + identifier in refs else "queued"
        row = {"id": identifier, "user_id": manifest["user_id"], "state": state, "knowledge": []}
        if receipt:
            row.update(state=receipt["state"])
            if "resolution" in receipt:
                row["resolution"] = receipt["resolution"]
            sources = {key for key, source in registry["sources"].items()
                       if any(item["run"] == identifier for item in source["acceptances"])}
            for key, entry in registry["entries"].items():
                version = entry["versions"][-1]
                if sources.intersection(version["evidence_ids"]):
                    row["knowledge"].append({"id": key, "status": version["status"],
                                             "statement": version["statement"], "reason": version["reason"]})
        elif state == "queued" and path.with_suffix(".sent").exists():
            row["state"] = "missing_remote"
        contributions.append(row)
    return {"state": "checked", "commit": commit, "contributions": contributions}


def run_git_team(config: GitTeamConfig, mode: str, accepted: list[Path], workspaces: list[Path],
                 result: dict, *, propose: TeamProposer | None = None) -> None:
    """Принимает режим и подключения; записывает итог Git-операции в result."""
    result["mode"] = mode
    with memory_lock(config.cache_dir):
        if mode == "status" and not checked_path(config.cache_dir, "outbox").exists():
            result.update(state="checked", contributions=[])
            return
        if mode == "status" and not checked_path(config.cache_dir, "team.json").is_file():
            raise ValueError("Очередь отправителя не привязана к Git-команде.")
        repo = open_repository(config)
        if mode == "process":
            if propose is None:
                raise ValueError("Командной обработке требуется исполнитель.")
            process_contributions(repo, config, propose, result)
        elif mode == "status":
            result.update(member_status(repo, config))
        elif mode == "sync":
            sync_git_team(repo, config, workspaces, result)
        elif mode == "contribute":
            submit_contributions(repo, config, accepted, result)
        else:
            raise ValueError("Неизвестная операция командной памяти.")
