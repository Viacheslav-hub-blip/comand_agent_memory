"""Локальная блокировка одновременного обновления выбранной области памяти.

Функция memory_lock принимает путь или идентификатор хранилища; возвращает
контекст блокировки ОС, освобождаемой при выходе из контекста или процесса.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile


@contextmanager
def memory_lock(target: Path | str, *, wait: bool = False):
    """Блокирует каталог на время обработки; вход — путь, выход — контекст без значения.

    Использует блокировку ОС: после завершения процесса она освобождается.
    По умолчанию другой писатель получает ошибку сразу. wait разрешает ожидание ОС.
    """
    identity = target if isinstance(target, str) else "files:" + os.path.normcase(str(target.resolve()))
    key = hashlib.sha256(os.fsencode(identity)).hexdigest()
    locks = Path(tempfile.gettempdir()) / "command-agent-memory-locks"
    locks.mkdir(parents=True, exist_ok=True)
    with (locks / f"{key}.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError(f"Каталог уже обрабатывается: {target}") from error
        yield
