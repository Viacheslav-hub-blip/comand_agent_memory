"""Журнал файловой публикации и восстановление после остановки процесса.

Функции: journal_path задаёт каталог журнала; durable_json сохраняет состояние;
check_hardlinks проверяет файловую систему до переноса оригиналов;
install_file заменяет ожидаемый файл без перезаписи конкурирующей записи;
rollback восстанавливает свои изменения, сохраняя внешние; recover_transaction
восстанавливает незавершённую публикацию; publish_transaction выполняет публикацию.
Классы отсутствуют.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from command_agent_memory.memory_storage.file_storage import FileMemoryStore


def journal_path(root: Path) -> Path:
    """Принимает корень памяти; возвращает соседний каталог журнала на том же диске."""
    key = hashlib.sha256(os.fsencode(os.path.normcase(str(root.resolve())))).hexdigest()[:24]
    return root.parent / (".memory-transaction-" + key)


def durable_json(path: Path, value: dict) -> None:
    """Принимает путь и объект; атомарно сохраняет JSON с принудительной записью файла."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def check_hardlinks(root: Path, directory: Path) -> None:
    """Принимает корень и журнал; проверяет атомарную установку ссылкой до изменения памяти."""
    root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".probe-", dir=directory)
    os.close(descriptor)
    target = root / (".memory-probe-" + uuid4().hex)
    linked = False
    try:
        os.link(temporary, target)
        linked = True
    finally:
        if linked:
            target.unlink(missing_ok=True)
        Path(temporary).unlink(missing_ok=True)


def install_file(path: Path, expected: bytes | None, replacement: bytes | None,
                 captured: Path) -> None:
    """Принимает файл, ожидаемые/новые байты и архив; заменяет без затирания чужого файла.

    Существующий файл сначала атомарно захватывается в архив. Новый устанавливается
    жёсткой ссылкой только на свободное имя. При гонке исходные байты остаются
    в архиве; чужой появившийся файл никогда не заменяется через os.replace.
    """
    captured.parent.mkdir(parents=True, exist_ok=True)
    if expected is not None:
        try:
            os.rename(path, captured)
        except FileNotFoundError as error:
            raise RuntimeError(f"Файл исчез перед записью: {path}") from error
        if captured.read_bytes() != expected:
            try:
                os.link(captured, path)
            except FileExistsError:
                pass
            raise RuntimeError(f"Файл изменился перед записью: {path}; сохранён в {captured}")
    if replacement is None:
        if path.exists():
            raise RuntimeError(f"Файл появился во время удаления: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".memory-", dir=captured.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise RuntimeError(f"Файл появился во время записи: {path}") from error
    finally:
        Path(temporary).unlink(missing_ok=True)


def rollback(store: "FileMemoryStore", state: dict, directory: Path) -> None:
    """Принимает хранилище, журнал и каталог; откатывает свою версию без затирания внешней.

    Неоднозначные архивные изменения сохраняются в журнале и блокируют чтение.
    Внешние правки действующих файлов сохраняются; они записываются в отчёт run.
    """
    from command_agent_memory.memory_storage.file_storage import checked_path

    before = {name: base64.b64decode(data, validate=True) for name, data in state["before"].items()}
    after = {name: base64.b64decode(data, validate=True) for name, data in state["after"].items()}
    conflicts = []
    for name in reversed(state["changed"]):
        path = checked_path(store.root, name)
        saved = checked_path(directory / "captured", name)
        undo = checked_path(directory / "undo", name)
        current = path.read_bytes() if path.is_file() else None
        original = saved.read_bytes() if saved.is_file() else before.get(name)
        # При остановке самого отката захваченный candidate остаётся в undo.
        if undo.is_file() and undo.read_bytes() != after.get(name):
            raise RuntimeError(f"В архиве отката есть внешние изменения: {undo}")
        if current == after.get(name) or (current is None and saved.is_file()):
            if undo.exists():
                undo.unlink()
            install_file(path, current, original, undo)
        elif current != original:
            conflicts.append(name)
            if saved.is_file() and original != before.get(name):
                raise RuntimeError(f"Нужна сверка двух внешних версий: {path} и {saved}")
    report = Path(state["run"]) / "recovery.json"
    durable_json(report, {"state": "rolled_back", "external_changes_preserved": conflicts})


def recover_transaction(store: "FileMemoryStore", *, allow_write: bool = True) -> None:
    """Принимает хранилище и разрешение записи; восстанавливает журнал перед чтением.

    Вызывается под блокировкой публикации. validate-only передаёт allow_write=False
    и получает ошибку при незавершённой публикации, не изменяя файлов.
    """
    from command_agent_memory.memory_storage.file_storage import checked_path

    directory = journal_path(store.root)
    checked_path(directory.parent, directory.name)
    if not directory.exists():
        return
    if not allow_write:
        raise RuntimeError(f"Нужно восстановить файловую публикацию: {directory}")
    manifest = checked_path(directory, "transaction.json")
    if not manifest.is_file():
        raise RuntimeError(f"Неполный журнал публикации; проверьте каталог: {directory}")
    state = json.loads(manifest.read_text(encoding="utf-8"))
    if (state.get("version") != 1 or state.get("root") != os.path.normcase(str(store.root.resolve()))
            or state.get("phase") not in ("applying", "committed")):
        raise ValueError(f"Некорректный журнал публикации: {directory}")
    for name in set(state["before"]) | set(state["after"]) | set(state["changed"]):
        checked_path(store.root, name)
    if state["phase"] != "committed":
        rollback(store, state, directory)
    # Каталог принадлежит этой публикации; ссылки внутри запрещены перед удалением.
    for path in directory.rglob("*"):
        checked_path(directory, path.relative_to(directory).as_posix())
    shutil.rmtree(directory)


def publish_transaction(store: "FileMemoryStore", before: dict[str, bytes], after: dict[str, bytes], run: Path) -> None:
    """Принимает хранилище, снимки и run; публикует разницу с устойчивым журналом.

    Блокировку удерживает FileMemoryStore. После аварийной остановки следующая
    операция хранилища восстанавливает before; после committed сохраняет after.
    Несогласованные внешние записи вызывают остановку и сохраняются без затирания.
    """
    from command_agent_memory.memory_storage.file_storage import checked_path
    from command_agent_memory.memory_storage.memory_snapshots import write_snapshot

    directory = journal_path(store.root)
    recover_transaction(store)
    if store._snapshot() != before:
        raise RuntimeError("Память рабочего агента изменилась; перенос отменён.")
    write_snapshot(run / "external-backup", before)
    changed = [name for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)]
    if not changed:
        return
    state = {"version": 1, "root": os.path.normcase(str(store.root.resolve())), "run": str(run.resolve()),
             "phase": "applying", "changed": changed,
             "before": {name: base64.b64encode(data).decode("ascii") for name, data in before.items()},
             "after": {name: base64.b64encode(data).decode("ascii") for name, data in after.items()}}
    durable_json(directory / "transaction.json", state)
    try:
        check_hardlinks(store.root, directory)
        for name in changed:
            install_file(checked_path(store.root, name), before.get(name), after.get(name),
                         checked_path(directory / "captured", name))
        if store._snapshot() != after:
            raise RuntimeError("Память изменилась во время публикации.")
        for name in changed:
            saved = directory / "captured" / name
            if saved.is_file() and saved.read_bytes() != before.get(name):
                raise RuntimeError(f"Внешний писатель изменил захваченный файл: {name}")
        state["phase"] = "committed"
        durable_json(directory / "transaction.json", state)
    except BaseException:
        recover_transaction(store)
        raise
    recover_transaction(store)
