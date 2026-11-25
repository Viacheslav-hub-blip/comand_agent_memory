"""Доступ к Git-объектам командной памяти без рабочего checkout.

Классы: GitCommandError сообщает ошибку команды; GitRepository хранит bare-кэш.
Методы GitRepository: __init__ создаёт кэш, run запускает Git, refs читает
удалённые ссылки, fetch получает commit, parent читает его родителя,
tree читает файлы, read_blobs получает объекты одним запросом, commit собирает
новый commit, push отправляет его обычным fast-forward.
Функция git_error_reason классифицирует stderr без раскрытия его содержимого.
"""
from io import BytesIO
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory


OBJECT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


GIT_ERROR_REASONS = (
    (("non-fast-forward", "fetch first"), "удалённая ветка изменилась"),
    (("protected branch", "pre-receive hook declined"), "сервер отклонил запись в ветку"),
    (("authentication failed", "could not read username", "permission denied", "returned error: 403"),
     "проверьте учётные данные Git и права доступа"),
    (("could not resolve host", "could not resolve hostname", "connection timed out", "failed to connect"),
     "не удалось подключиться к серверу Git"),
    (("couldn't find remote ref", "does not appear to be a git repository", "repository not found"),
     "репозиторий или ветка недоступны"),
)


def git_error_reason(stderr: bytes) -> str:
    """Принимает stderr Git. Возвращает известную причину без путей, адресов и секретов."""
    message = stderr.decode("utf-8", errors="replace").lower()
    for patterns, reason in GIT_ERROR_REASONS:
        if any(pattern in message for pattern in patterns):
            return f" ({reason})"
    return ""


class GitCommandError(RuntimeError):
    """Принимает безопасное описание операции, завершившейся ошибкой Git."""


class GitRepository:
    """Принимает bare-кэш и адрес команды; предоставляет операции с обычным Git."""

    def __init__(self, cache: Path, remote: str):
        """Запоминает адрес и создаёт каталог Git-объектов; возвращает None."""
        self.cache = cache
        self.remote = remote
        self.run("init", "--bare", "--quiet", "--template=", "--", str(cache))

    def run(self, *args: str, input_data: bytes | None = None,
            index: Path | None = None) -> bytes:
        """Принимает аргументы и необязательный ввод; возвращает stdout без декодирования."""
        environment = dict(os.environ)
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_COMMON_DIR"):
            environment.pop(key, None)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        if index is not None:
            environment["GIT_INDEX_FILE"] = str(index)
        command = ["git", "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
                   "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
                   "-c", "user.name=Command Agent Memory",
                   "-c", "user.email=command-agent-memory@noreply.invalid",
                   "--no-replace-objects"]
        if args[0] != "init":
            command.append(f"--git-dir={self.cache}")
        try:
            result = subprocess.run([*command, *args], env=environment, input=input_data,
                                    capture_output=True, check=False, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GitCommandError(f"Git {args[0]}: {type(error).__name__}.") from None
        if result.returncode:
            reason = git_error_reason(result.stderr)
            raise GitCommandError(f"Git {args[0]} завершился с кодом {result.returncode}{reason}.")
        return result.stdout

    def refs(self, *patterns: str) -> dict[str, str]:
        """Принимает шаблоны ссылок; возвращает имена удалённых refs и идентификаторы объектов."""
        result = {}
        for line in self.run("ls-remote", "--refs", "--", self.remote, *patterns).decode("utf-8").splitlines():
            oid, ref = line.split("\t", 1)
            if not OBJECT_ID.fullmatch(oid):
                raise ValueError("Git вернул некорректный идентификатор объекта.")
            result[ref] = oid
        return result

    def fetch(self, ref: str) -> str:
        """Принимает удалённую ссылку; получает и возвращает её commit."""
        self.run("fetch", "--no-tags", "--", self.remote, ref)
        return self.run("rev-parse", "--verify", "FETCH_HEAD^{commit}").decode("ascii").strip()

    def parent(self, commit: str) -> str:
        """Принимает SHA commit с одним родителем; возвращает SHA родителя."""
        if not OBJECT_ID.fullmatch(commit):
            raise ValueError("Для проверки родителя нужен SHA commit.")
        parents = self.run("rev-list", "--parents", "-n", "1", commit).decode("ascii").split()
        if len(parents) != 2:
            raise ValueError("Вклад должен состоять из commit с одним родителем.")
        return parents[1]

    def tree(self, commit: str) -> dict[str, bytes]:
        """Принимает SHA commit; возвращает текстовые или бинарные байты обычных файлов."""
        if not OBJECT_ID.fullmatch(commit):
            raise ValueError("Для чтения дерева нужен SHA commit.")
        output = self.run("ls-tree", "-r", "-z", commit)
        entries, portable = {}, set()
        for entry in output.split(b"\0"):
            if not entry:
                continue
            header, path = entry.split(b"\t", 1)
            mode, kind, oid = header.decode("ascii").split()
            name = path.decode("utf-8")
            key = name.casefold()
            if mode != "100644" or kind != "blob" or key in portable:
                raise ValueError("Дерево Git содержит ссылку, исполняемый или неоднозначный путь.")
            entries[name] = oid
            portable.add(key)
        blobs = self.read_blobs(tuple(dict.fromkeys(entries.values())))
        return {name: blobs[oid] for name, oid in entries.items()}

    def read_blobs(self, identifiers: tuple[str, ...]) -> dict[str, bytes]:
        """Принимает SHA объектов. Возвращает их байты из одного запроса Git."""
        if not identifiers:
            return {}
        if not all(OBJECT_ID.fullmatch(oid) for oid in identifiers):
            raise ValueError("Для чтения объектов Git нужны полные SHA.")
        request = ("\n".join(identifiers) + "\n").encode("ascii")
        stream = BytesIO(self.run("cat-file", "--batch", input_data=request))
        blobs = {}
        for oid in identifiers:
            header = stream.readline().split()
            if (len(header) != 3 or header[:2] != [oid.encode("ascii"), b"blob"]
                    or not header[2].isdigit()):
                raise ValueError("Git вернул некорректный заголовок объекта.")
            size = int(header[2])
            content = stream.read(size)
            if len(content) != size or stream.read(1) != b"\n":
                raise ValueError("Git вернул неполный объект.")
            blobs[oid] = content
        return blobs

    def commit(self, parent: str, files: dict[str, bytes], message: str) -> str:
        """Принимает базовый commit и полный набор файлов; возвращает SHA нового commit."""
        if not OBJECT_ID.fullmatch(parent):
            raise ValueError("Для публикации нужен проверенный базовый commit.")
        with TemporaryDirectory(dir=self.cache) as temporary:
            index = Path(temporary) / "index"
            entries = bytearray()
            for name, content in sorted(files.items()):
                oid = self.run("hash-object", "-w", "--stdin", input_data=content).strip()
                entries.extend(b"100644 " + oid + b"\t" + name.encode("utf-8") + b"\0")
            self.run("update-index", "-z", "--index-info", index=index, input_data=entries)
            tree = self.run("write-tree", index=index).decode("ascii").strip()
        return self.run("commit-tree", tree, "-p", parent, "-m", message).decode("ascii").strip()

    def push(self, commit: str, ref: str) -> None:
        """Принимает SHA и удалённую ветку; отправляет commit, возвращает None."""
        if not OBJECT_ID.fullmatch(commit) or not ref.startswith("refs/heads/"):
            raise ValueError("Публиковать можно только commit в явно заданную ветку.")
        self.run("push", "--no-verify", "--", self.remote, f"{commit}:{ref}")
