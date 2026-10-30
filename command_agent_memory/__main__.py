"""Запуск конвейера по конфигурации пользователя.

Функция main принимает аргументы CLI, проверяет параметры и запускает конвейер.
Классы отсутствуют.
"""
import argparse
from pathlib import Path

from command_agent_memory.memory_pipeline import run_pipeline


def main(argv: list[str] | None = None) -> None:
    """Принимает аргументы CLI; запускает конфигурационный конвейер, возвращает None."""
    parser = argparse.ArgumentParser(description="Личная и командная память агентов")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--evaluate-by-replay", action="store_true")
    args = parser.parse_args(argv)
    run_pipeline(args.config, validate_only=args.validate_only,
                 evaluate_by_replay=args.evaluate_by_replay)


if __name__ == "__main__":
    main()
