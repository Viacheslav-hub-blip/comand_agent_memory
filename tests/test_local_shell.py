"""Тесты устойчивого backend локальной командной строки.

Функции: test_decode_shell_output_utf8 — проверка UTF-8;
test_decode_shell_output_cp866 — проверка консольной кодировки Windows.
test_invalid_virtual_path_returns_tool_error — восстановимая ошибка пути;
test_valid_virtual_path_is_writable — сохранение записи внутри области.
test_timeout_terminates_python_child — тайм-аут с завершением дочернего процесса.
"""
import unittest
import ctypes
import os
import sys
import time
from pathlib import Path
import tempfile
from unittest.mock import patch

from command_agent_memory.model_execution.local_shell import RobustLocalShellBackend, decode_shell_output


class RobustLocalShellTests(unittest.TestCase):
    """Проверяет декодирование вывода локальных процессов; входов конструктора нет."""

    def test_decode_shell_output_utf8(self) -> None:
        """Передаёт UTF-8 и проверяет возврат исходного русского текста."""
        self.assertEqual(decode_shell_output("готово".encode("utf-8")), "готово")

    def test_decode_shell_output_cp866(self) -> None:
        """Передаёт CP866 и проверяет возврат исходного русского текста."""
        self.assertEqual(decode_shell_output("Проверка".encode("cp866")), "Проверка")

    def test_invalid_virtual_path_returns_tool_error(self) -> None:
        """Передаёт отказ проверки границ и traversal; проверяет ошибку вместо записи."""
        with tempfile.TemporaryDirectory() as directory:
            backend = RobustLocalShellBackend(root_dir=directory, virtual_mode=True)
            with patch("deepagents.backends.LocalShellBackend._resolve_path", side_effect=ValueError("outside root")):
                result = backend.write("/result.txt", "Данные")
            self.assertIn("outside root", result.error)
            self.assertIsNotNone(backend.write("/../outside.txt", "Данные").error)
            self.assertFalse((Path(directory) / "result.txt").exists())

    def test_valid_virtual_path_is_writable(self) -> None:
        """Передаёт разрешённый путь и текст; проверяет успешную запись внутри области."""
        with tempfile.TemporaryDirectory() as directory:
            backend = RobustLocalShellBackend(root_dir=directory, virtual_mode=True)
            result = backend.write("/outputs/result.txt", "Данные")
            self.assertIsNone(result.error)
            self.assertEqual((Path(directory) / "outputs/result.txt").read_text(encoding="utf-8"), "Данные")

    @unittest.skipUnless(os.name == "nt", "Проверяется дерево процессов Windows")
    def test_timeout_terminates_python_child(self) -> None:
        """Запускает спящий Python; проверяет код 124, ограниченное время и остановку потомка."""
        with tempfile.TemporaryDirectory() as directory:
            backend = RobustLocalShellBackend(root_dir=directory, virtual_mode=True)
            command = (f'"{sys.executable}" -c "import os,time; '
                       "from pathlib import Path; Path('child.pid').write_text(str(os.getpid())); time.sleep(30)\"")
            started = time.monotonic()
            response = backend.execute(command, timeout=2)
            self.assertEqual(response.exit_code, 124)
            self.assertLess(time.monotonic() - started, 8)
            child_pid = int((Path(directory) / "child.pid").read_text())
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, child_pid)
            if handle:
                try:
                    code = ctypes.c_ulong()
                    ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                    self.assertNotEqual(code.value, 259)
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
