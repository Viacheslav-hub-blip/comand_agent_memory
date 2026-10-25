"""Проверка автоматического сценария из истории Codex без вызовов модели.

Вход: временные manifest, история, вход и результат.
Выход: результаты unittest.
Класс CodexReplayAdapterTests содержит test_build_scenario — создание полного сценария.
"""
import json
from pathlib import Path
import tempfile
import unittest

from codex_replay_adapter import build_replay_scenario
from memory_pipeline import ROOT
from replay_evaluation import load_replay_scenario


class CodexReplayAdapterTests(unittest.TestCase):
    """Принимает временные файлы; проверяет выбор запроса, входов и результата."""

    def test_build_scenario(self):
        """Без входов создаёт сценарий из одного диалога и проверяет его; возвращает None."""
        temporary_root = ROOT / "tmp"
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
            root = Path(directory)
            reports = root / "reports/session-one"
            reports.mkdir(parents=True)
            source = root / "source.txt"
            result = root / "result.txt"
            source.write_text("input", encoding="utf-8")
            result.write_text("output", encoding="utf-8")
            history = root / "history.jsonl"
            records = [
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text":
                              f"# Files mentioned by the user:\n## source.txt: {source}\n"
                              "## My request:\nОбработай файл"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": f"Готово: [результат]({result})"}]}},
            ]
            history.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in records), encoding="utf-8")
            manifest = {
                "state": "completed", "source": "codex", "session_id": "one",
                "history": str(history), "source_session": {"project": "demo"},
            }
            (reports / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            scenario_path = build_replay_scenario(root / "reports", root / "scenario")
            scenario = load_replay_scenario(scenario_path)
            self.assertEqual(scenario.request, "Обработай файл")
            self.assertEqual((scenario.inputs / "source.txt").read_text(), "input")
            self.assertIn("Готово", (scenario.reference / "answer.md").read_text(encoding="utf-8"))
            self.assertEqual((scenario.reference / "files/result.txt").read_text(), "output")


if __name__ == "__main__":
    unittest.main()
