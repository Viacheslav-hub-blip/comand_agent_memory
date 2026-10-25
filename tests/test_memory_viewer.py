"""Проверяет чтение нативных историй для HTML без запуска браузера.

Класс MemoryViewerTests: test_native_roles_and_text — полнота текста и роли;
test_incomplete_analysis — отсутствие смешивания незавершённых результатов.
"""
import json
from pathlib import Path
import tempfile
import unittest

from build_memory_viewer import codex_chats


class MemoryViewerTests(unittest.TestCase):
    """Принимает сценарии unittest; проверяет материалы HTML по исходным событиям."""

    def test_native_roles_and_text(self):
        """Без входов проверяет реплики, инструмент и сжатие без потери текста; возвращает None."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / 'history.jsonl'
            records = [
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                 'content': [{'type': 'input_text', 'text': 'Первый блок'}, {'type': 'input_text', 'text': 'Второй блок'}]}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                 'content': [{'type': 'output_text', 'text': 'Ответ агента'}]}},
                {'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'Длинный результат ' * 3000}},
                {'type': 'compacted', 'payload': {'message': 'Контекст сжатия'}},
            ]
            history.write_text('\n'.join(json.dumps(record, ensure_ascii=False) for record in records), encoding='utf-8')
            session = root / 'session-id'
            session.mkdir()
            (session / 'manifest.json').write_text(json.dumps({
                'state': 'completed', 'history': str(history), 'session_id': 'id',
                'source_session': {'project': 'jev_test', 'title': 'Задача'}
            }), encoding='utf-8')
            messages = codex_chats(root)[0]['messages']
            self.assertEqual([m['role'] for m in messages], ['user', 'ai', 'tool', 'system'])
            self.assertEqual(messages[0]['text'], 'Первый блок\n\nВторой блок')
            self.assertEqual(messages[1]['text'], 'Ответ агента')
            self.assertEqual(json.loads(messages[2]['text'])['output'], records[2]['payload']['output'])
            self.assertEqual(json.loads(messages[3]['text'])['message'], 'Контекст сжатия')

    def test_incomplete_analysis(self):
        """Без входов проверяет запрет сборки незавершённого анализа; возвращает None."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / 'session-id'
            session.mkdir()
            (session / 'manifest.json').write_text('{"state":"running"}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'не завершён'):
                codex_chats(root)


if __name__ == '__main__':
    unittest.main()
