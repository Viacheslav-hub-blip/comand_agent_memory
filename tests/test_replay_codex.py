"""Проверка выбора модели Codex и сохранения измерений одного прогона без сети.

Класс CodexReplayTests содержит test_model_memory_and_metrics — передача модели,
памяти и usage; test_failure_keeps_evidence — сохранение данных при сбое.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from replay_codex import build_replay_prompt, run_codex_replay


class CodexReplayTests(unittest.TestCase):
    """Принимает сценарии unittest; проверяет запуск Codex без внешних обращений."""

    def test_model_memory_and_metrics(self):
        """Без входов проверяет модель, чтение памяти и измеренные токены; возвращает None."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            request = root / "request.md"
            request.write_text("Посчитай локальные данные", encoding="utf-8")
            result = root / "result"

            def execute(executable, model, prompt, cwd, prefix, writable=False):
                """Принимает параметры CLI; пишет события и метрики, возвращает ответ без сети."""
                self.assertEqual(model, "gpt-5.6-terra")
                self.assertEqual(cwd, workspace)
                self.assertTrue(writable)
                self.assertIn("knowledge/skills/", prompt)
                self.assertIn("только для чтения", prompt)
                prefix.with_suffix(".jsonl").write_text(json.dumps({
                    "type": "item.completed", "item": {"type": "command_execution"},
                }) + "\n", encoding="utf-8")
                prefix.with_suffix(".metrics.json").write_text(json.dumps({
                    "completed": True, "duration_seconds": 2,
                    "usage": {"input_tokens": 12, "output_tokens": 3},
                }), encoding="utf-8")
                return "Получено 42"

            with patch("replay_codex.run_codex", side_effect=execute) as call:
                run_codex_replay(workspace, request, result, "codex", "gpt-5.6-terra")
            self.assertEqual(call.call_count, 1)
            metrics = json.loads((result / "metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(metrics["usage"]["input_tokens"], 12)
            self.assertIsNone(metrics["model_calls"])
            self.assertEqual(metrics["tool_calls"], 1)
            self.assertTrue(metrics["usage_complete"])
            self.assertEqual((result / "answer.md").read_text(encoding="utf-8"), "Получено 42")
            self.assertTrue((result / "transcript.jsonl").is_file())

    def test_failure_keeps_evidence(self):
        """Без входов проверяет передачу сбоя и отсутствие выдуманного usage; возвращает None."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = root / "request.md"
            request.write_text("Задача", encoding="utf-8")
            with patch("replay_codex.run_codex", side_effect=RuntimeError("connection failed")) as call:
                with self.assertRaisesRegex(RuntimeError, "connection failed"):
                    run_codex_replay(root, request, root / "result", "codex", "gpt-5.6-terra")
            self.assertEqual(call.call_count, 1)
            metrics = json.loads((root / "result/metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(metrics["state"], "failed")
            self.assertIsNone(metrics["usage"])
            self.assertFalse(metrics["usage_complete"])

    def test_baseline_prompt_excludes_memory(self):
        """Без входов проверяет baseline без упоминания и загрузки памяти; возвращает None."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = root / "request.md"
            request.write_text("Задача", encoding="utf-8")
            prompt = build_replay_prompt(root, request, use_memory=False)
        self.assertNotIn("Сначала прочитай", prompt)
        self.assertIn("не должны использоваться", prompt)


if __name__ == "__main__":
    unittest.main()
