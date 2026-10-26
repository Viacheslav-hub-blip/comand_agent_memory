"""Устойчивое выполнение локальных команд DeepAgents в Windows.

Функции: decode_shell_output — декодирование байтов stdout/stderr с резервными
Windows-кодировками. Классы: RobustLocalShellBackend — backend командной строки,
который не завершает поток чтения при выводе не в UTF-8 и может защищать память
проверочного прогона от записи.
"""
from __future__ import annotations

import subprocess

from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import EditResult, ExecuteResponse, WriteResult


WINDOWS_FALLBACK_ENCODINGS = ("cp866", "cp1251")


def decode_shell_output(value: bytes | None) -> str:
    """Принимает байты вывода процесса и возвращает декодированную строку.

    Сначала используется UTF-8, затем распространённые кодировки консоли Windows.
    Если ни одна строгая попытка не удалась, UTF-8 заменяет только повреждённые
    последовательности, чтобы вызывающий агент всегда получил диагностический текст.
    """
    if not value:
        return ""
    for encoding in ("utf-8", *WINDOWS_FALLBACK_ENCODINGS):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="replace")


class RobustLocalShellBackend(LocalShellBackend):
    """Выполняет локальные команды и возвращает вывод без ошибок декодирования.

    Входные параметры конструктора дополняются защищёнными путями. Методы записи
    отклоняют изменение этих файлов и каталогов. Метод ``execute`` принимает
    команду и необязательный тайм-аут, а возвращает ``ExecuteResponse`` с
    объединёнными stdout и stderr.
    """

    def __init__(self, *args, read_only_paths: tuple[str, ...] = (), **kwargs) -> None:
        """Принимает параметры LocalShellBackend и виртуальные пути только для чтения."""
        super().__init__(*args, **kwargs)
        self._read_only_paths = tuple(
            self._resolve_path(path).resolve() for path in read_only_paths
        )

    def _is_read_only(self, file_path: str) -> bool:
        """Принимает виртуальный путь; возвращает принадлежность защищённой области."""
        try:
            resolved = self._resolve_path(file_path).resolve()
        except (OSError, RuntimeError):
            return False
        return any(resolved == root or root in resolved.parents for root in self._read_only_paths)

    def write(self, file_path: str, content: str) -> WriteResult:
        """Принимает путь и текст; создаёт файл либо возвращает запрет записи памяти."""
        if self._is_read_only(file_path):
            return WriteResult(error=f"Путь доступен только для чтения: {file_path}")
        return super().write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str,
             replace_all: bool = False) -> EditResult:
        """Принимает замену текста; изменяет файл либо возвращает запрет записи памяти."""
        if self._is_read_only(file_path):
            return EditResult(error=f"Путь доступен только для чтения: {file_path}")
        return super().edit(file_path, old_string, new_string, replace_all)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Принимает команду и тайм-аут, возвращает результат её выполнения.

        Потоки процесса читаются как байты и декодируются функцией
        ``decode_shell_output``. Команды с превышенным тайм-аутом возвращают код 124.
        """
        if not command or not isinstance(command, str):
            return ExecuteResponse(
                output="Error: Command must be a non-empty string.",
                exit_code=1,
                truncated=False,
            )
        effective_timeout = timeout if timeout is not None else self._default_timeout
        if effective_timeout <= 0:
            raise ValueError(f"timeout must be positive, got {effective_timeout}")
        try:
            result = subprocess.run(
                command,
                check=False,
                shell=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=False,
                timeout=effective_timeout,
                env=self._env,
                cwd=str(self.cwd),
            )
        except subprocess.TimeoutExpired:
            suffix = " (custom timeout)" if timeout is not None else ""
            return ExecuteResponse(
                output=f"Error: Command timed out after {effective_timeout} seconds{suffix}.",
                exit_code=124,
                truncated=False,
            )
        except Exception as error:  # noqa: BLE001
            return ExecuteResponse(
                output=f"Error executing command ({type(error).__name__}): {error}",
                exit_code=1,
                truncated=False,
            )

        stdout = decode_shell_output(result.stdout)
        stderr = decode_shell_output(result.stderr)
        output_parts = [stdout] if stdout else []
        if stderr:
            output_parts.extend(f"[stderr] {line}" for line in stderr.strip().splitlines())
        output = "\n".join(output_parts) if output_parts else "<no output>"
        truncated = len(output) > self._max_output_bytes
        if truncated:
            output = output[: self._max_output_bytes]
            output += f"\n\n... Output truncated at {self._max_output_bytes} bytes."
        if result.returncode != 0:
            output = f"{output.rstrip()}\n\nExit code: {result.returncode}"
        return ExecuteResponse(output=output, exit_code=result.returncode, truncated=truncated)

