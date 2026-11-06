"""Область записи и бюджет одного модельного задания без изменения контракта Runner.

ExecutionPolicy хранит разрешённые пути и ограничения. execution_policy временно
меняет их в текущем контексте; current_execution_policy возвращает текущие значения.
run_bounded_process выполняет дочерний процесс с остановкой дерева по тайм-ауту.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import os
import signal
import subprocess


@dataclass(frozen=True)
class ExecutionPolicy:
    """Принимает пути записи и бюджеты; хранит политику одного модельного задания."""

    write_paths: tuple[str, ...] | None = None
    timeout_seconds: int | None = None
    max_model_calls: int | None = None


_POLICY = ContextVar("execution_policy", default=ExecutionPolicy())


def current_execution_policy() -> ExecutionPolicy:
    """Без аргументов возвращает действующую политику текущего модельного задания."""
    return _POLICY.get()


@contextmanager
def execution_policy(**changes):
    """Принимает изменяемые поля политики; выдаёт её временный контекст и восстанавливает прежний."""
    policy = replace(_POLICY.get(), **changes)
    for value in (policy.timeout_seconds, policy.max_model_calls):
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError("Ограничения исполнения должны быть положительными целыми числами.")
    token = _POLICY.set(policy)
    try:
        yield policy
    finally:
        _POLICY.reset(token)


def run_bounded_process(command: list[str], payload: str, timeout: int, *, cwd=None,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE) -> subprocess.CompletedProcess:
    """Принимает команду, строковый вход и секунды; возвращает вывод либо TimeoutExpired.

    По тайм-ауту останавливает дерево процесса. До возврата собирает stdout и stderr.
    """
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout,
                               stderr=stderr, cwd=cwd, text=True, encoding="utf-8", errors="replace",
                               start_new_session=os.name != "nt")
    try:
        stdout, stderr = process.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10, check=False)
            else:
                os.killpg(process.pid, signal.SIGKILL)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
