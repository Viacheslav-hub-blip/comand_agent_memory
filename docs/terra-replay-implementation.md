# Прогон через Codex с выбранной моделью

Добавлен выбор исполнителя `--replay-backend codex`. По умолчанию остаётся DeepAgents. Старые и новые ответы остаются разделены; на пользователя выполняется один новый запрос.

## replay_codex.py: run_codex_replay

```python
def run_codex_replay(workspace: Path, request: Path, result: Path,
                     executable: str, model: str) -> None:
    """Принимает копию окружения, запрос, каталог результата, CLI и модель; возвращает None.

    Агент получает только задачу, входные файлы и новую память. Прежний ответ
    остаётся вне рабочей области. Число внутренних обращений к модели CLI
    не сообщает: model_calls сохраняется как null, а не как число turns.
    При ошибке сохраняются доступная трасса и измерения, исключение передаётся выше.
    """
    result.mkdir(parents=True, exist_ok=False)
    prefix = result / "agent"
    prompt = (
        f"Рабочая область: {workspace.as_posix()}. Реши один запрос пользователя. "
        "Используй только файлы этой рабочей области; сеть и внешние сервисы не используй. "
        "Сначала прочитай существующие AGENTS.md, USER_PREFERENCES.md, user_llm_wiki/INDEX.md "
        "и knowledge/INDEX.md. Найди подходящие skills в skills/ и knowledge/skills/ "
        "и прочитай применимые инструкции и статьи по ссылкам. Память используй только "
        "для чтения. Результаты задачи сохраняй внутри рабочей области. Если в запросе "
        "указаны пути вида /documents/ или /synthetic_team/, разрешай их относительно "
        "физического корня этой рабочей области. Исходные данные не изменяй. "
        "В итоговом ответе сообщи полученный результат и созданные файлы.\n\n"
        "Запрос пользователя:\n" + request.read_text(encoding="utf-8")
    )
    state = "failed"
    try:
        answer = run_codex(executable, model, prompt, workspace, prefix, writable=True)
        if not answer.strip():
            raise RuntimeError("Codex не выдал итоговый ответ задачи.")
        (result / "answer.md").write_text(answer, encoding="utf-8")
        state = "completed"
    finally:
        event_path = prefix.with_suffix(".jsonl")
        events = []
        if event_path.is_file():
            shutil.copy2(event_path, result / "transcript.jsonl")
            events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        measurements = prefix.with_suffix(".metrics.json")
        source = json.loads(measurements.read_text(encoding="utf-8")) if measurements.is_file() else {}
        tools = [event["item"] for event in events if event.get("type") == "item.completed"
                 and event.get("item", {}).get("type") in
                 ("command_execution", "mcp_tool_call", "web_search", "file_change")]
        usage = source.get("usage")
        metrics = {
            "state": state, "backend": "codex", "model": model,
            "duration_seconds": source.get("duration_seconds"),
            "model_calls": None, "tool_calls": len(tools),
            "tool_calls_scope": "completed CLI command, MCP, search and file-change items",
            "usage": usage, "usage_complete": bool(source.get("completed") and usage is not None),
        }
        (result / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
```

## replay_evaluation.py: evaluate_by_replay

```python
def evaluate_by_replay(proposal: MemoryProposal, scenario: ReplayScenario,
                       executable: str, model: str, agent_python: Path,
                       user_id: str, backend: str = "deepagents") -> dict:
    """Принимает предложение, сценарий, CLI, модель, Python и пользователя; возвращает выбор LLM.

    backend выбирает DeepAgents либо Codex с переданной моделью.
    Один раз запускает решающего агента в копии. Прежние ответы передаются только
    оценщику. Любой сбой вызывает исключение без переноса памяти. Выбор current
    или inconclusive также не является разрешением переноса.
    """
    if backend not in ("deepagents", "codex"):
        raise ValueError("Неизвестный backend проверочного прогона.")
    if scenario.inputs is None or scenario.reference is None or not scenario.request.strip():
        raise ValueError("Для выполнения прогона нужен запрос, inputs и reference.")
    directory = proposal.run / "replay"
    directory.mkdir()
    workspace = directory / "workspace"
    write_snapshot(workspace, proposal.after)
    copy_directory(scenario.inputs, workspace / scenario.inputs.name)
    if (proposal.target / "skills").is_dir():
        copy_directory(proposal.target / "skills", workspace / "skills")
    copy_directory(scenario.reference, directory / "reference")
    (directory / "request.md").write_text(scenario.request, encoding="utf-8")
    (directory / "reference-metrics.json").write_text(
        json.dumps(scenario.reference_metrics, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    command = [
        str(agent_python), "-X", "utf8", str(ROOT / "replay_worker.py"),
        "--workspace", str(workspace), "--request", str(directory / "request.md"),
        "--result", str(directory / "result"), "--user-id", user_id,
    ]
    result = None
    started = perf_counter()
    print(f"Один проверочный прогон: {directory}", flush=True)
    try:
        if backend == "codex":
            from replay_codex import run_codex_replay

            run_codex_replay(workspace, directory / "request.md", directory / "result", executable, model)
            result = subprocess.CompletedProcess([], 0)
        else:
            with (directory / "worker.stdout.log").open("w", encoding="utf-8") as stdout:
                with (directory / "worker.stderr.log").open("w", encoding="utf-8") as stderr:
                    result = subprocess.run(command, cwd=ROOT, stdout=stdout, stderr=stderr, timeout=900)
    finally:
        (directory / "execution.json").write_text(json.dumps({
            "backend": backend, "model": model if backend == "codex" else None,
            "duration_seconds": perf_counter() - started,
            "return_code": result.returncode if result is not None else None,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"Прогон не завершился. См. {directory / 'worker.stderr.log'}")
    metrics = json.loads((directory / "result/metrics.json").read_text(encoding="utf-8"))
    if metrics.get("state") != "completed" or not (directory / "result/answer.md").is_file():
        raise RuntimeError("Прогон не сохранил завершённый результат.")
    if read_memory(workspace, "personal") != proposal.after:
        raise RuntimeError("Агент изменил память во время прогона; перенос отменён.")
    files = directory / "result/files"
    files.mkdir()
    for path in workspace.iterdir():
        if path.name in (*PERSONAL_MEMORY_ENTRIES, "skills"):
            continue
        if path.is_dir():
            copy_directory(path, files / path.name)
        else:
            if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError(f"Ссылка в результате не допускается: {path}")
            shutil.copy2(path, files / path.name)
    skill = ROOT / "agents/evaluate-replay/skills/evaluate-replay/SKILL.md"
    (directory / "evaluation").mkdir()
    prompt = (
        skill.read_text(encoding="utf-8")
        + f"\n\nЗапрос: {(directory / 'request.md').as_posix()}. "
        + f"Исходные данные: {scenario.inputs.as_posix()}. "
        + f"Прежний результат: {(directory / 'reference').as_posix()}. "
        + f"Прежние измерения: {(directory / 'reference-metrics.json').as_posix()}. "
        + f"Новый результат, файлы, трасса и измерения: {(directory / 'result').as_posix()}. "
        + "Сравни результаты. Файлы не изменяй. Верни JSON по skill."
    )
    reply = run_codex(executable, model, prompt, directory / "evaluation", directory / "evaluation/judge")
    decision = json.loads(reply)
    if (not isinstance(decision, dict) or decision.get("choice") not in ("candidate", "current", "inconclusive")
            or not isinstance(decision.get("summary"), str) or not decision["summary"].strip()):
        raise RuntimeError("Неверный протокол решения оценщика прогона.")
    (directory / "decision.json").write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    return decision
```

## reviewed_consolidation.py: consolidate_with_review

```python
def consolidate_with_review(target: Path, stage: str, materials: str, skill_path: Path,
                            executable: str, model: str, run: Path,
                            review_model: str | None = None,
                            knowledge_materials: str = "",
                            replay_scenario: "ReplayScenario | None" = None,
                            agent_python: Path | None = None, user_id: str = "replay",
                            replay_requested: bool = False, replay_context: Path | None = None,
                            replay_backend: str = "deepagents") -> None:
    """Принимает память, материалы, skills и модели; переносит принятую копию, возвращает None.

    Личный этап готовит правила, контекст и knowledge в одной копии. Блокировка
    удерживается от чтения до переноса; отказ и сбой сохраняют исходную память.
    replay_requested или replay_scenario включает оценку возможности прогона.
    LLM выбирает run/skip по сценарию и replay_context. При skip перенос разрешает
    ревью; после выполнения требуется candidate. agent_python выбирает Python,
    user_id — автора сообщений. replay_backend выбирает DeepAgents или Codex.
    Отчёты этапов сохраняются в run/report/.
    """
    target, run = target.resolve(), run.resolve()
    replay_enabled = replay_requested or replay_scenario is not None
    if replay_enabled and stage != "personal":
        raise ValueError("Проверочный прогон применяется только к личному этапу.")
    if (stage not in ("personal", "knowledge") or target == ROOT or run == ROOT
            or not target.is_relative_to(ROOT) or not run.is_relative_to(ROOT)
            or target.is_relative_to(run) or run.is_relative_to(target)):
        raise RuntimeError("Память и материалы запуска должны быть раздельными каталогами внутри проекта.")
    run.mkdir(parents=True, exist_ok=False)
    status = {"stage": stage, "target": str(target), "state": "running",
              "replay_status": "pending" if replay_enabled else "not_requested"}
    try:
        with memory_lock(target):
            links = {"Полные извлечённые сведения": run / "materials.md"}
            if replay_context is not None:
                links["Входные отчёты и журналы"] = replay_context
            write_stage_report(run, "analysis", "Извлечённые сведения", "completed",
                               "Переданы результаты анализа контекста и решений. Их полные тексты "
                               "сохраняются в materials.md без разбора разделов.", links=links)
            proposal = prepare_memory(
                target, stage, materials, skill_path, executable, model, run, knowledge_materials,
            )
            before_review = proposal.after
            write_stage_report(run, "review", "Проверка и исправления", "running", "Ревьюер проверяет копию памяти.")
            proposal, decision = review_memory(proposal, executable, review_model or model)
            write_stage_report(run, "review", "Проверка и исправления", decision["decision"],
                               decision.get("summary", "Решение сохранено в журнале ревью."),
                               before_review, proposal.after,
                               {"Предложение до ревью": run / "before-review", "Результат ревью": run / "after",
                                "Решение": run / "reviews/review-001.json"})
            status["reviews"] = decision["reviews"]
            if decision["decision"] != "accepted":
                status.update(state="rejected", review=decision["review"])
                raise RuntimeError(f"Память не обновлена: {decision['summary']} См. {decision['review']}")
            if replay_enabled:
                from replay_evaluation import assess_replay, evaluate_by_replay

                write_stage_report(run, "replay-assessment", "Возможность и стоимость прогона", "running",
                                   "LLM оценивает доступные материалы без выполнения задачи.")
                assessment = assess_replay(proposal, replay_scenario, executable, review_model or model,
                                           replay_context, agent_python or Path(sys.executable))
                status["replay_assessment"] = str(run / "replay-assessment/decision.json")
                context_text = assessment["context"] if isinstance(assessment["context"], str) else json.dumps(
                    assessment["context"], ensure_ascii=False, indent=2)
                resource_text = assessment["resources"] if isinstance(assessment["resources"], str) else json.dumps(
                    assessment["resources"], ensure_ascii=False, indent=2)
                write_stage_report(run, "replay-assessment", "Возможность и стоимость прогона", "completed",
                                   assessment["summary"] + "\n\nКонтекст:\n" + context_text
                                   + "\n\nЗатраты:\n" + resource_text,
                                   links={"Решение LLM": run / "replay-assessment/decision.json"})
                if assessment["decision"] == "skip":
                    status.update(replay_status="skipped", replay_reason=assessment["summary"])
                    write_stage_report(run, "replay", "Проверка решением задачи", "skipped",
                                       assessment["summary"] + "\n\nПрогон не выполнен. Принятие памяти основано на ревью.")
                else:
                    if replay_scenario is None:
                        raise RuntimeError("LLM разрешил прогон без предоставленного сценария.")
                    status.update(replay_status="running", replay=str(run / "replay"))
                    write_stage_report(run, "replay", "Проверка решением задачи", "running", "Выполняется один прогон.")
                    try:
                        evaluation = evaluate_by_replay(
                            proposal, replay_scenario, executable, review_model or model,
                            agent_python or Path(sys.executable), user_id,
                            backend=replay_backend,
                        )
                    except BaseException:
                        status["replay_status"] = "failed"
                        raise
                    status.update(replay_status="completed", replay_choice=evaluation["choice"])
                    write_stage_report(run, "replay", "Проверка решением задачи", "completed",
                                       f"Выбор: {evaluation['choice']}\n\n{evaluation['summary']}",
                                       links={"Сравнение": run / "replay/decision.json", "Результат": run / "replay/result"})
                    if evaluation["choice"] != "candidate":
                        status["state"] = "rejected"
                        raise RuntimeError(f"Память не обновлена после прогона: {evaluation['summary']}")
            else:
                write_stage_report(run, "replay", "Проверка решением задачи", "not_requested",
                                   "Проверка не запрошена. Принятие памяти основано на ревью.")
            if read_memory(run / "after", stage) != proposal.after:
                raise RuntimeError("Проверяемая копия изменилась; перенос отменён.")
            if proposal.after != proposal.before:
                publish_memory(target, proposal.before, proposal.after, stage, run)
            elif read_memory(target, stage) != proposal.before:
                raise RuntimeError("Действующая память изменилась; результат устарел.")
            status["state"] = "published" if proposal.after != proposal.before else "unchanged"
    except BaseException as error:
        status.setdefault("error", str(error))
        if status["state"] == "running":
            status["state"] = "failed"
        index = run / "report/index.json"
        if index.exists():
            for name, entry in json.loads(index.read_text(encoding="utf-8")).items():
                if entry["state"] == "running":
                    write_stage_report(run, name, entry["title"], "failed", str(error))
        raise
    finally:
        if status["replay_status"] == "not_requested" and not (run / "report/replay.md").exists():
            write_stage_report(run, "replay", "Проверка решением задачи", "not_requested",
                               "Проверка решением задачи не запрошена.")
        if status["replay_status"] == "pending":
            status["replay_status"] = "not_run"
            write_stage_report(run, "replay", "Проверка решением задачи", "not_run",
                               "Прогон не запускался: обработка остановлена до решения о его выполнении.")
        (run / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        applied = status["state"] in ("published", "unchanged")
        write_stage_report(run, "publication", "Принятие памяти", status["state"],
                           ("Память принята. Область: " + str(target) if applied else
                            "Память не принята. " + status.get("error", "")),
                           proposal.before if applied else None, proposal.after if applied else None,
                           {"Состояние запуска": run / "status.json", "Память до изменений": run / "before",
                            "Память после ревью": run / "after"})
```
