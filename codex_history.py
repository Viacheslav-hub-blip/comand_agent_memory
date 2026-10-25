"""Адаптер локальных историй Codex без импорта в SQLite проекта.

Функции: project_name — имя каталога; list_sessions — поиск в индексе Codex;
remove_embedded_binary — удаление встроенных бинарных данных;
export_session — чтение JSONL/Zstandard с сохранением событий диалога.
Для сжатых логов требуется пакет zstandard.
"""
from contextlib import closing, ExitStack
import io
import json
from pathlib import Path, PureWindowsPath
import sqlite3


def remove_embedded_binary(value):
    """Принимает JSON-значение; возвращает копию без строк data URI с base64.

    Словари и списки обходятся рекурсивно. Обычный текст сохраняется без изменений.
    Вместо бинарной части остаётся тип данных и число удалённых символов.
    """
    if isinstance(value, dict):
        return {key: remove_embedded_binary(item) for key, item in value.items()}
    if isinstance(value, list):
        return [remove_embedded_binary(item) for item in value]
    if isinstance(value, str) and value.startswith("data:") and ";base64," in value:
        header, encoded = value.split(",", 1)
        return f"[{header}; удалено {len(encoded)} символов base64]"
    return value


def project_name(cwd: str) -> str:
    """Принимает cwd из индекса; возвращает имя проекта для Windows или POSIX."""
    return (PureWindowsPath(cwd).name if '\\' in cwd else Path(cwd).name).casefold()


def list_sessions(home: Path, projects: list[str], limit: int = 2) -> list[dict]:
    """Читает state_5.sqlite только для чтения; возвращает последние limit чатов каждого проекта.

    Вход: каталог Codex, имена проектов, число диалогов на проект.
    Выход: метаданные и существующие пути JSONL или JSONL.zst.
    Если проект либо файл не найден, вызывает исключение вместо пропуска.
    """
    with closing(sqlite3.connect((home / 'state_5.sqlite').resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            'SELECT id,cwd,rollout_path,title,updated_at FROM threads '
            'ORDER BY updated_at DESC,id'
        ).fetchall()
    result = []
    for project in projects:
        selected = [row for row in rows if project_name(row['cwd']) == project.casefold()][:limit]
        if not selected:
            raise ValueError(f'Не найдены диалоги проекта {project}')
        for row in selected:
            path = Path(row['rollout_path'])
            if not path.is_file():
                path = Path(str(path) + '.zst')
            if not path.is_file():
                raise FileNotFoundError(f"Нет лога диалога {row['id']}: {path}")
            result.append({**dict(row), 'project': project, 'path': str(path)})
    return result


def export_session(session: dict, output: Path) -> int:
    """Читает нативный лог; пишет события сообщений, инструментов и сжатия, возвращает их число.

    Поля выбранных записей остаются без преобразований. Исключает телеметрию,
    дубли event_msg и reasoning. Повреждённый JSON вызывает исключение.
    Исходный лог открывается только для чтения; выходной путь задаёт вызывающий код.
    """
    path = Path(session['path'])
    count = 0
    with ExitStack() as stack:
        raw = stack.enter_context(path.open('rb'))
        if path.suffix == '.zst':
            try:
                import zstandard
            except ImportError as error:
                raise RuntimeError('Для логов .zst установи zstandard в используемое окружение') from error
            raw = stack.enter_context(zstandard.ZstdDecompressor().stream_reader(raw))
        stream = stack.enter_context(io.TextIOWrapper(raw, encoding='utf-8'))
        target = stack.enter_context(output.open('w', encoding='utf-8'))
        for line in stream:
            record = json.loads(line)
            kind = record.get('type')
            payload = record.get('payload', {})
            if kind in ('session_meta', 'compacted') or (
                kind == 'response_item' and payload.get('type') != 'reasoning'
            ):
                target.write(json.dumps(remove_embedded_binary(record), ensure_ascii=False) + '\n')
                count += 1
    return count
