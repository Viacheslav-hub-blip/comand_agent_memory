"""Консолидация отчётов пользователя через Codex CLI либо OpenRouter.

Вход: обязательные --user-id и --workspace; --reports-dir, --knowledge-dir, --stage, --model,
--codex-path; полные отчёты контекста и решений, skills консолидации.
Выход: принятая память либо отклонённая копия, журналы и report/summary.md каждого этапа.
По умолчанию читает raw/chat-reports-codex/ и выполняет оба этапа последовательно.
Личный этап готовит правила, контекст и knowledge в одной копии до общего ревью.
--evaluate-by-replay включает LLM-оценку возможности и затрат до одного прогона.
Без сценария или при его неполноте LLM может выбрать пропуск с объяснением.
--agent-python выбирает Python решающего агента с DeepAgents.
--replay-backend codex использует выбранную модель Codex и для решения задачи.
--validate-only проверяет входные данные без запуска Codex и записи файлов.
Функций и классов нет. Используются ROOT и read_reports из memory_pipeline.py,
consolidate_with_review из reviewed_consolidation.py. Изменения готовятся в копии;
качество содержания и ссылки проверяет LLM. Журналы: data/consolidation-runs/.
"""
import argparse
from datetime import datetime
from pathlib import Path
import sys
from uuid import uuid4

from codex_client import find_codex, run_codex
from openrouter_client import run_openrouter, validate_openrouter
from memory_pipeline import ROOT, read_reports
from reviewed_consolidation import KNOWLEDGE_SKILL, consolidate_with_review
from replay_evaluation import load_replay_scenario


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--user-id", required=True, help="Идентификатор пользователя")
parser.add_argument("--workspace", type=Path, required=True, help="Область памяти пользователя")
parser.add_argument("--reports-dir", type=Path, help="Каталог отчётов анализа")
parser.add_argument("--knowledge-dir", type=Path, default=Path("knowledge"))
parser.add_argument("--stage", choices=("personal", "knowledge", "both"), default="both")
parser.add_argument("--model", default="gpt-5.6-luna", help="Идентификатор модели выбранного backend")
parser.add_argument("--backend", choices=("codex", "openrouter"), default="codex",
                    help="Исполнитель анализа, консолидации, ревью и оценки")
parser.add_argument("--review-model", help="Модель ревьюера; по умолчанию совпадает с --model")
parser.add_argument("--codex-path", type=Path, help="Путь к исполняемому файлу Codex")
parser.add_argument("--validate-only", action="store_true", help="Без запуска Codex")
parser.add_argument("--evaluate-by-replay", action="store_true", help="Оценить возможность и при необходимости выполнить один прогон")
parser.add_argument("--replay-scenario", type=Path, help="JSON с запросом, входами и прежним результатом")
parser.add_argument("--force-replay", action="store_true", help="Обязательно выполнить полный сценарий без оценки пропуска")
parser.add_argument("--agent-python", type=Path, help="Python с DeepAgents; по умолчанию текущий")
parser.add_argument("--replay-backend", choices=("deepagents", "codex", "openrouter"),
                    default="deepagents", help="Исполнитель проверочного прогона")
args = parser.parse_args()
replay_scenario = None
agent_python = None
if args.evaluate_by_replay:
    if args.stage == "knowledge":
        parser.error("Проверка прогоном используется с personal/both.")
    if args.replay_scenario is not None:
        try:
            replay_scenario = load_replay_scenario(ROOT / args.replay_scenario, allow_incomplete=True)
        except (ValueError, OSError) as error:
            parser.error(str(error))
    if args.force_replay and replay_scenario is None:
        parser.error("--force-replay требует полный --replay-scenario.")
    agent_python = (args.agent_python or Path(sys.executable)).expanduser().resolve()
    for name in ("assess-replay", "evaluate-replay"):
        if not (ROOT / f"agents/{name}/skills/{name}/SKILL.md").is_file():
            parser.error(f"Не найден skill {name}.")
elif (args.replay_scenario is not None or args.agent_python is not None
      or args.replay_backend != "deepagents" or args.force_replay):
    parser.error("--replay-scenario и --agent-python используются с --evaluate-by-replay.")
if not args.user_id or not all(c.isalnum() or c in "_-" for c in args.user_id):
    parser.error("user-id должен содержать буквы, цифры, _ или -")
workspace = (ROOT / args.workspace).resolve()
report_folder = "chat-reports-openrouter" if args.backend == "openrouter" else "chat-reports-codex"
reports_directory = ((ROOT / args.reports_dir).resolve() if args.reports_dir
                     else workspace / "raw" / report_folder)
knowledge_directory = (ROOT / args.knowledge_dir).resolve()
for path in (workspace, reports_directory, knowledge_directory):
    if not path.is_relative_to(ROOT) or path == ROOT:
        parser.error(f"Укажи каталог внутри проекта: {path}")
if not workspace.is_dir():
    parser.error(f"Область пользователя не найдена: {workspace}")
if (knowledge_directory.is_relative_to(workspace)
        or workspace.is_relative_to(knowledge_directory)):
    parser.error("Личная область и каталог знаний не должны пересекаться")
if (knowledge_directory.is_relative_to(reports_directory)
        or reports_directory.is_relative_to(knowledge_directory)):
    parser.error("Каталог знаний и входные отчёты не должны пересекаться")
try:
    context_reports, procedure_reports, session_count = read_reports(reports_directory)
except (OSError, ValueError, RuntimeError) as error:
    raise SystemExit(f"Не удалось прочитать отчёты: {error}") from error
if not session_count:
    raise SystemExit(f"Нет отчётов в {reports_directory}. Сначала запусти анализ.")
knowledge_reports = "\n\n".join(part for part in (context_reports, procedure_reports) if part)

tasks = []
if args.stage in ("personal", "both"):
    tasks.append((
        "personal", ROOT / "agents/consolidation/skills/consolidation/SKILL.md",
        workspace, "Область памяти пользователя", context_reports, procedure_reports,
    ))
if args.stage in ("knowledge", "both"):
    tasks.append((
        "knowledge", KNOWLEDGE_SKILL,
        knowledge_directory, "Каталог знаний", knowledge_reports, "",
    ))

for stage, skill_path, target, label, materials, personal_knowledge in tasks:
    try:
        skill_path.read_text(encoding="utf-8-sig")
        if stage in ("personal", "knowledge"):
            KNOWLEDGE_SKILL.read_text(encoding="utf-8-sig")
            (KNOWLEDGE_SKILL.parent / "references/skill-authoring.md").read_text(encoding="utf-8-sig")
        (ROOT / "agents/review-memory/skills/review-memory/SKILL.md").read_text(encoding="utf-8-sig")
    except OSError as error:
        raise SystemExit(f"Не удалось прочитать инструкции: {error}") from error

print(f"Пользователь: {args.user_id}; диалогов: {session_count}; "
      f"backend: {args.backend}; модель: {args.model}")
print(f"Отчёты: {reports_directory}")
print(f"Личная память: {workspace}\nКаталог знаний: {knowledge_directory}")
print("Этапы: " + (", ".join(task[0] for task in tasks) or "нет сведений"))
if args.evaluate_by_replay:
    if not any(task[0] == "personal" for task in tasks):
        parser.error("Нет материалов для личного этапа и проверочного прогона.")
    print("Перед прогоном LLM оценит контекст и затраты; возможен пропуск с объяснением.")
if args.validate_only or not tasks:
    raise SystemExit(0)

try:
    if args.backend == "openrouter":
        validate_openrouter()
        executable, runner = "", run_openrouter
    else:
        executable, runner = find_codex(args.codex_path), run_codex
except (FileNotFoundError, RuntimeError, ValueError) as error:
    raise SystemExit(str(error)) from error
print("Исполнитель: OpenRouter API" if args.backend == "openrouter" else f"Codex CLI: {executable}")
run_id = datetime.now().strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:12]
log_directory = (ROOT / "data/consolidation-runs" / run_id).resolve()
if not log_directory.is_relative_to(ROOT):
    raise SystemExit("Каталог журналов должен находиться внутри проекта.")
log_directory.mkdir(parents=True, exist_ok=True)

for stage, skill_path, target, label, materials, personal_knowledge in tasks:
    print(f"\nЭтап: {stage}; {label}: {target}; копии и решения: {log_directory / stage}", flush=True)
    try:
        consolidate_with_review(
            target, stage, materials, skill_path, executable, args.model,
            log_directory / stage, review_model=args.review_model,
            knowledge_materials=personal_knowledge,
            replay_scenario=replay_scenario if stage == "personal" else None,
            agent_python=agent_python, user_id=args.user_id,
            replay_requested=args.evaluate_by_replay and stage == "personal",
            replay_context=reports_directory,
            replay_backend=args.replay_backend,
            force_replay=args.force_replay,
            runner=runner,
        )
    except Exception as error:
        raise SystemExit(f"Этап {stage} остановлен: {error}\nМатериалы: {log_directory / stage}"
                         f"\nОтчёты: {log_directory / stage / 'report/summary.md'}") from error
    print(f"Этап принят; действующая память: {target}", flush=True)
    print(f"Отчёты этапа: {log_directory / stage / 'report/summary.md'}", flush=True)
print(f"\nГотово. Журналы: {log_directory}")
