"""Проверяет адаптер Codex без сетевых вызовов.

Класс CodexHistoryTests: test_project_names — пути разных ОС;
test_native_events — сохранение сообщений и инструментов;
test_embedded_binary_is_removed — исключение base64 при сохранении текста;
test_selection — отдельный лимит проектов и сжатые файлы.
"""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_history import export_session, list_sessions, project_name, remove_embedded_binary


class CodexHistoryTests(unittest.TestCase):
    """Принимает сценарии unittest; возвращает результаты проверок чтения логов."""

    def test_project_names(self):
        """Без входов проверяет имена каталогов Windows и POSIX; возвращает None."""
        self.assertEqual(project_name(r'\\?\C:\work\JEV_TEST'), 'jev_test')
        self.assertEqual(project_name('/work/offzone/'), 'offzone')

    def test_native_events(self):
        """Без входов проверяет полные payload и неизменность источника; возвращает None."""
        records = [
            {'type': 'session_meta', 'payload': {'id': 'a'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': ['Привет']}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'arguments': '{"x":1}'}},
            {'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'Полный результат'}},
            {'type': 'compacted', 'payload': {'message': 'Контекст'}},
            {'type': 'response_item', 'payload': {'type': 'reasoning'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count'}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'log.jsonl', Path(directory) / 'out.jsonl'
            source.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in records), encoding='utf-8')
            before = source.read_bytes()
            self.assertEqual(export_session({'path': str(source)}, target), 5)
            self.assertEqual([json.loads(line) for line in target.read_text(encoding='utf-8').splitlines()], records[:5])
            self.assertEqual(source.read_bytes(), before)

    def test_selection(self):
        """Без входов проверяет лимит каждого проекта и отсутствие тихого пропуска файлов; возвращает None."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / 'state_5.sqlite')) as connection:
                connection.execute('CREATE TABLE threads(id,cwd,rollout_path,title,updated_at)')
                for i, project in enumerate(('a', 'a', 'b')):
                    path = root / f'{i}.jsonl'
                    Path(str(path) + '.zst').touch()
                    connection.execute('INSERT INTO threads VALUES(?,?,?,?,?)', (str(i), '/work/' + project, str(path), '', i))
                connection.commit()
            sessions = list_sessions(root, ['a', 'b'], 1)
            self.assertEqual([s['id'] for s in sessions], ['1', '2'])
            self.assertTrue(all(s['path'].endswith('.zst') for s in sessions))
            self.assertEqual(len(list_sessions(root, ['a', 'b'], None)), 3)
            with self.assertRaises(ValueError):
                list_sessions(root, ['missing'])

    def test_embedded_binary_is_removed(self):
        """Без входов проверяет удаление data URI и сохранение соседнего текста; возвращает None."""
        value = {"content": ["Описание", {"image_url": "data:image/png;base64,QUJDRA=="}]}
        cleaned = remove_embedded_binary(value)
        self.assertEqual(cleaned["content"][0], "Описание")
        self.assertIn("удалено 8 символов base64", cleaned["content"][1]["image_url"])


if __name__ == '__main__':
    unittest.main()
