"""Собирает HTML с полными текстами чатов и памяти без изменения исходников.

Функции: document — чтение файла; documents — чтение каталога Markdown;
chats — чтение SQLite; codex_chats — чтение историй Codex;
codex_bundle — сбор личного прогона; bundle — сбор команды; main — запись страницы.
"""

import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_RUN = ROOT / 'data/refactor-steps-1-3/20261024T060531Z'


def document(path: Path) -> dict:
    """Принимает путь UTF-8 файла; возвращает его абсолютный путь и полный текст."""
    return {'path': str(path.resolve()), 'text': path.read_text(encoding='utf-8-sig')}


def documents(directory: Path) -> list:
    """Принимает каталог; возвращает все Markdown-файлы с полными текстами."""
    return [document(path) for path in sorted(directory.rglob('*.md'))]


def chats(database: Path, user_id: str) -> list:
    """Читает SQLite только для чтения; возвращает диалоги пользователя со всеми сообщениями."""
    result = {}
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            'SELECT * FROM messages WHERE user_id=? ORDER BY id', (user_id,)
        )
        for row in rows:
            message = json.loads(row['message_json'])
            data = message.get('data', {})
            content = data.get('content', row['content'])
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2)
            calls = data.get('tool_calls') or []
            if calls:
                text += '\n\nВызовы инструментов:\n' + json.dumps(calls, ensure_ascii=False, indent=2)
            session = result.setdefault(row['session_id'], [])
            session.append({'role': row['role'], 'name': data.get('name'),
                            'time': row['created_at'], 'text': text})
    finally:
        connection.close()
    return [{'id': key, 'messages': value, 'path': str(database.resolve())}
            for key, value in result.items()]


def bundle(run: Path) -> dict:
    """Принимает каталог эксперимента с manifest.json; возвращает материалы четырёх участников."""
    manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8-sig'))
    source = ROOT / manifest['source_run']
    people = [('101', 'Анна Лебедева', 'Бизнес-аналитик'),
              ('102', 'Борис Ким', 'Инженер данных'),
              ('103', 'Вера Соколова', 'Аналитик антифрода'),
              ('104', 'Денис Орлов', 'Руководитель аналитики')]
    skills = []
    for name in ('analyze-context', 'analyze-procedures', 'consolidation',
                 'consolidate-knowledge', 'review-memory', 'evaluate-replay'):
        skills.extend(documents(ROOT / 'agents' / name / 'skills'))
    users = []
    for uid, name, role in people:
        workspace = source / 'users' / uid / 'user_workspace'
        user = {'id': uid, 'name': name, 'role': role,
                'chats': chats(workspace / 'chat_logs.db', uid),
                'analysis': [], 'rounds': []}
        reports = run / 'users' / uid / 'user_workspace/raw/chat-reports-codex'
        for session in user['chats']:
            for kind in ('context', 'procedures'):
                path = reports / ('session-' + session['id']) / (kind + '.md')
                if path.exists():
                    user['analysis'].append(document(path))
        for key, label in [('first', 'Первый прогон'), ('repeat', 'Повтор'),
                           ('corrected_repeat', 'Повтор после правок skills')]:
            location = manifest.get('rounds', {}).get(key, {}).get(uid)
            if not location:
                continue
            directory = ROOT / location
            snapshots = {stage: documents(directory / stage)
                         for stage in ('before', 'before-review', 'after')}
            outputs = [document(directory / filename) for filename in
                       ('consolidation.last.txt', 'consolidation-knowledge.last.txt',
                        'review.md', 'rules.md', 'review-SKILL.md', 'materials.md',
                        'consolidation.prompt.md', 'consolidation-knowledge.prompt.md')
                       if (directory / filename).exists()]
            user['rounds'].append({'name': label, 'snapshots': snapshots, 'outputs': outputs})
        users.append(user)
    audits = [document(run / name) for name in
              ('assessment.md', 'evaluation/quality-audit.md', 'evaluation/repeat-audit.md',
               'evaluation/final-audit.md') if (run / name).exists()]
    return {'users': users, 'skills': skills, 'audits': audits, 'run': str(run.resolve())}


def codex_chats(reports: Path) -> list:
    """Принимает каталог отчётов; возвращает полные события Codex с ролями для просмотра.

    Читает именно истории, использованные анализаторами, по manifest.json.
    Содержимое инструментов и сжатий сохраняется целиком в текстовом виде.
    """
    sessions = []
    for path in sorted(reports.glob('session-*/manifest.json')):
        manifest = json.loads(path.read_text(encoding='utf-8'))
        if manifest['state'] != 'completed':
            raise ValueError(f'Анализ ещё не завершён: {path}')
        messages = []
        history = Path(manifest['history'])
        for line in history.read_text(encoding='utf-8').splitlines():
            record = json.loads(line)
            payload = record.get('payload', {})
            kind = payload.get('type')
            if record['type'] == 'response_item' and kind == 'message':
                role = {'assistant': 'ai'}.get(payload['role'], payload['role'])
                content = payload.get('content', [])
                text = content if isinstance(content, str) else '\n\n'.join(
                    block.get('text', json.dumps(block, ensure_ascii=False, indent=2))
                    for block in content)
            else:
                role = 'tool' if record['type'] == 'response_item' else 'system'
                text = json.dumps(payload, ensure_ascii=False, indent=2)
            messages.append({'role': role, 'name': payload.get('name') or kind,
                             'time': record.get('timestamp', ''), 'text': text})
        source = manifest['source_session']
        sessions.append({'id': manifest['session_id'], 'messages': messages,
                         'path': str(history), 'project': source['project'],
                         'title': source['title']})
    return sessions


def codex_bundle(workspace: Path, consolidation_run: Path) -> dict:
    """Принимает личную область и каталог personal прогона; возвращает данные одной HTML-страницы."""
    reports = workspace / 'raw/chat-reports-codex'
    snapshots = {stage: documents(consolidation_run / stage)
                 for stage in ('before', 'before-review', 'after')}
    names = ('consolidation.last.txt', 'consolidation-knowledge.last.txt', 'review.md',
             'rules.md', 'review-SKILL.md', 'materials.md', 'consolidation.prompt.md',
             'consolidation-knowledge.prompt.md', 'status.json',
             'reviews/review-001.prompt.md', 'reviews/review-001.json')
    outputs = [document(consolidation_run / name) for name in names
               if (consolidation_run / name).is_file()]
    skills = []
    for name in ('analyze-context', 'analyze-procedures', 'consolidation',
                 'consolidate-knowledge', 'review-memory'):
        skills.extend(documents(ROOT / 'agents' / name / 'skills'))
    user = {'id': workspace.name, 'name': 'Слава', 'role': 'jev_test и offzone',
            'chats': codex_chats(reports), 'analysis': [],
            'rounds': [{'name': 'Личная консолидация с ревью',
                        'snapshots': snapshots, 'outputs': outputs}]}
    for session in user['chats']:
        for kind in ('context', 'procedures'):
            user['analysis'].append(document(reports / ('session-' + session['id']) / (kind + '.md')))
    status = json.loads((consolidation_run / 'status.json').read_text(encoding='utf-8'))
    return {'users': [user], 'skills': skills, 'audits': [], 'run': str(consolidation_run),
            'notice': f"Личный прогон по четырём диалогам Codex. Статус консолидации: {status['state']}. "
                      f'Область памяти: {workspace.resolve()}. '
                      'Снимок до ревью содержит результат двух консолидаторов. '
                      'Проверка повторным решением задачи не выполнялась.'}


def main() -> None:
    """Принимает CLI-пути эксперимента и HTML; записывает автономную страницу и печатает её путь."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, default=DEFAULT_RUN)
    parser.add_argument('--output', type=Path, default=ROOT / 'diagrams/participants-memory.html')
    parser.add_argument('--workspace', type=Path, help='Область одного пользователя с отчётами Codex')
    parser.add_argument('--consolidation-run', type=Path, help='Каталог personal завершённой консолидации')
    args = parser.parse_args()
    if bool(args.workspace) != bool(args.consolidation_run):
        parser.error('--workspace и --consolidation-run указываются вместе')
    materials = codex_bundle(args.workspace, args.consolidation_run) if args.workspace else bundle(args.run_dir)
    payload = json.dumps(materials, ensure_ascii=False).replace('<', '\\u003c')
    template = (ROOT / 'docs/memory-viewer-template.html').read_text(encoding='utf-8')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(template.replace('__MEMORY_DATA__', payload), encoding='utf-8')
    print(args.output.resolve())


if __name__ == '__main__':
    main()
