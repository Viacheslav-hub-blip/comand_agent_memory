"""Обмен локальными заданиями и ответами с агентом-хостом.

Функции: write_json атомарно записывает объект; save_reply принимает ответ.
HostTasks: __init__ задаёт каталог, request публикует задание и ждёт ответ,
adapter подключает модельные этапы, run выполняет один модельный этап.
"""
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic, sleep, time
from uuid import uuid4

from command_agent_memory.memory_storage.memory_locking import memory_lock
from command_agent_memory.model_execution.execution_policy import current_execution_policy
from command_agent_memory.model_execution.provider_registry import ModelAdapter
from command_agent_memory.model_execution.runtime_environment import describe_execution_environment


def write_json(path: Path, value: dict) -> None:
    """Принимает путь и объект; атомарно публикует UTF-8 JSON, возвращает None."""
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_reply(task: Path, answer: Path | None, error: str | None) -> None:
    """Принимает задание и файл ответа либо ошибку; однократно сохраняет своевременный результат."""
    task = task.resolve(strict=True)
    if task.name != "task.json" or not task.is_file():
        raise ValueError("Нужен путь к task.json.")
    value = {"answer": answer.read_text(encoding="utf-8-sig")} if answer else {"error": error}
    with memory_lock(task, wait=True):
        destination = task.with_name("reply.json")
        if task.with_name("finished").is_file():
            raise ValueError("Задание завершено.")
        if time() >= json.loads(task.read_text(encoding="utf-8"))["expires_at"]:
            raise TimeoutError("Срок ответа на задание истёк.")
        if destination.exists():
            raise FileExistsError("Ответ на это задание уже отправлен.")
        write_json(destination, value)


class HostTasks:
    """Принимает каталог одного процесса; передаёт задания агенту через файлы."""

    def __init__(self, directory: Path):
        """Принимает каталог запуска; сохраняет его абсолютный путь."""
        self.directory = directory.resolve()

    def request(self, stage: str, model: str, workspace: Path, prompt: Path,
                write_paths: tuple[str, ...] = (), **details) -> str:
        """Принимает этап, пути и параметры задания; возвращает первый своевременный ответ."""
        directory = self.directory / uuid4().hex
        directory.mkdir()
        task = directory / "task.json"
        response = directory / "reply.json"
        timeout = current_execution_policy().timeout_seconds or 900
        deadline = monotonic() + timeout
        write_json(task, {
            "stage": stage, "model": model, "workspace": str(workspace.resolve()), "prompt": str(prompt.resolve()),
            "write_paths": list(write_paths), "timeout_seconds": timeout, "expires_at": time() + timeout,
            **details,
        })
        try:
            while not response.is_file() and monotonic() < deadline:
                sleep(0.1)
        finally:
            with memory_lock(task, wait=True):
                (directory / "finished").touch()
        if not response.is_file():
            raise TimeoutError(f"Агент не ответил на этап {stage} за {timeout} с.")
        value = json.loads(response.read_text(encoding="utf-8"))
        if value.get("error"):
            raise RuntimeError(str(value["error"]))
        answer = value.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError(f"Агент вернул пустой ответ на этап {stage}.")
        return answer

    def adapter(self, _executable: Path | None) -> ModelAdapter:
        """Принимает неиспользуемый путь CLI; возвращает адаптер агентских заданий."""
        return ModelAdapter("native", "host", self.run, self.run)

    def run(self, _executable: str, model: str, prompt: str, cwd: Path, prefix: Path,
            writable: bool = False) -> str:
        """Принимает запрос этапа; возвращает текст агента и записывает доступные измерения."""
        policy = current_execution_policy()
        stage = json.loads(prefix.with_suffix(".stage.json").read_text(encoding="utf-8"))["stage"]
        prompt_file = prefix.with_suffix(".prompt.md")
        prompt_file.write_text(describe_execution_environment(cwd.resolve()) + "\n\n" + prompt, encoding="utf-8")
        write_paths = policy.write_paths if policy.write_paths is not None else (str(cwd.resolve()),)
        started = monotonic()
        try:
            answer = self.request(stage, model, cwd, prompt_file, write_paths if writable else ())
            prefix.with_suffix(".last.txt").write_text(answer, encoding="utf-8")
            return answer
        finally:
            write_json(prefix.with_suffix(".metrics.json"), {
                "model": model, "wall_seconds": monotonic() - started,
                "model_calls": None, "tool_calls": None, "usage": None,
            })
