"""Тесты устойчивого backend локальной командной строки.

Функции: test_decode_shell_output_utf8 — проверка UTF-8;
test_decode_shell_output_cp866 — проверка консольной кодировки Windows.
"""
import unittest

from robust_local_shell import decode_shell_output


class RobustLocalShellTests(unittest.TestCase):
    """Проверяет декодирование вывода локальных процессов; входов конструктора нет."""

    def test_decode_shell_output_utf8(self) -> None:
        """Передаёт UTF-8 и проверяет возврат исходного русского текста."""
        self.assertEqual(decode_shell_output("готово".encode("utf-8")), "готово")

    def test_decode_shell_output_cp866(self) -> None:
        """Передаёт CP866 и проверяет возврат исходного русского текста."""
        self.assertEqual(decode_shell_output("Проверка".encode("cp866")), "Проверка")


if __name__ == "__main__":
    unittest.main()
