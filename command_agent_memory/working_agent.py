"""Штатная сборка локального DeepAgents-агента для работы и replay.

Функции: create_agent возвращает рабочий граф; create_session собирает тот же граф
с профилем модели, инструментов и среды; tool_profile описывает фактические инструменты.
"""
import hashlib
from importlib.metadata import version
import inspect
import locale
import os
from pathlib import Path
import platform
import sys
from typing import TYPE_CHECKING

from command_agent_memory.memory_storage.memory_layout import MemoryLayout
from command_agent_memory.model_execution.runtime_environment import describe_execution_environment
from command_agent_memory.worker_agents import WorkerSession

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph


def tool_profile(agent: "CompiledStateGraph") -> list[dict]:
    """Принимает собранный граф; возвращает реальные имена, схемы и версии реализаций инструментов."""
    node = agent.nodes.get("tools")
    tools = node.bound.tools_by_name if node is not None else {}
    result = []
    for name, tool in sorted(tools.items()):
        schema = tool.args_schema
        schema = schema.model_json_schema() if hasattr(schema, "model_json_schema") else schema or {}
        function = getattr(tool, "func", None) or getattr(tool, "coroutine", None)
        try:
            source = inspect.getsourcefile(function) if function is not None else None
        except TypeError:
            source = None
        implementation = ((getattr(function, "__module__", "") + ":" + getattr(function, "__qualname__", "")
                           + ":" + hashlib.sha256(Path(source).read_bytes()).hexdigest())
                          if source else type(tool).__module__ + ":" + type(tool).__qualname__)
        result.append({"name": name, "schema": schema, "description": tool.description,
                       "implementation": implementation})
    return result


def create_session(workspace: Path, options: dict, memory_layout: MemoryLayout | None = None,
                   *, tools: list | None = None, read_only_paths: tuple[str, ...] = (),
                   memory_enabled: bool = True) -> WorkerSession:
    """Принимает рабочий каталог, настройки и штатные tools; возвращает новую сессию с профилем.

    options явно задаёт model; необязательные model_parameters, run_config и system_prompt
    одинаково применяются в работе и replay. Shell выполняется локально с правами
    пользователя. read_only_paths ограничивает файловые инструменты, но не shell.
    Фабрика не отправляет запрос модели. Внешние tools передаёт фабрика проекта.
    """
    from deepagents import create_deep_agent
    from langchain_openai import ChatOpenAI
    from langgraph.checkpoint.memory import InMemorySaver
    from command_agent_memory.model_execution.local_shell import RobustLocalShellBackend
    from command_agent_memory.model_execution.openrouter_api import (
        openrouter_api_key, OPENROUTER_TIMEOUT_SECONDS, OPENROUTER_MAX_RETRIES,
    )

    if (set(options) - {"model", "model_parameters", "system_prompt", "run_config"}
            or not isinstance(options.get("model"), str) or not options["model"]
            or not isinstance(options.get("model_parameters", {}), dict)
            or set(options.get("model_parameters", {})) - {"temperature", "top_p", "seed", "max_tokens"}
            or not isinstance(options.get("system_prompt", ""), str)):
        raise ValueError("Для штатного агента нужны model и допустимые model_parameters/system_prompt.")
    run_config = options.get("run_config", {})
    if (not isinstance(run_config, dict) or set(run_config) - {"recursion_limit", "max_concurrency"}
            or any(type(value) is not int or value < 1 for value in run_config.values())):
        raise ValueError("run_config допускает положительные recursion_limit и max_concurrency.")
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise FileNotFoundError(workspace)
    parameters = options.get("model_parameters", {})
    model = ChatOpenAI(model=options["model"], api_key=openrouter_api_key(),
                       base_url="https://openrouter.ai/api/v1", timeout=OPENROUTER_TIMEOUT_SECONDS,
                       max_retries=OPENROUTER_MAX_RETRIES, **parameters)
    backend = RobustLocalShellBackend(
        root_dir=workspace, virtual_mode=True, inherit_env=True, read_only_paths=read_only_paths,
        env={"PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
             "PYTHONUTF8": "1", "PYTHONIOENCODING": locale.getpreferredencoding(False) + ":backslashreplace"},
    )
    memory, skills = [], []
    if memory_enabled:
        personal = memory_layout.personal if memory_layout is not None else (
            "AGENTS.md", "USER_PREFERENCES.md", "user_llm_wiki")
        knowledge = memory_layout.knowledge if memory_layout is not None else ("knowledge", "team_knowledge")
        for name in (*personal, *knowledge):
            path = workspace / name
            if path.is_file():
                memory.append("/" + name)
            elif (path / "INDEX.md").is_file():
                memory.append("/" + name + "/INDEX.md")
        for name in ("skills", *(value + "/skills" for value in knowledge)):
            if (workspace / name).is_dir():
                skills.append("/" + name + "/")
    prompt = options.get("system_prompt") or describe_execution_environment(workspace, file_paths="virtual", shell=True)
    agent = create_deep_agent(model=model, backend=backend, tools=tools or [], memory=memory,
                              skills=skills, system_prompt=prompt, checkpointer=InMemorySaver())
    if run_config:
        agent = agent.with_config(run_config)
    normalized_prompt = prompt.replace(workspace.as_posix(), "<workspace>").replace(str(workspace), "<workspace>")
    profile = {
        "model": options["model"], "model_parameters": {**parameters, "timeout": OPENROUTER_TIMEOUT_SECONDS,
                                                           "max_retries": OPENROUTER_MAX_RETRIES},
        "system_prompt_sha256": hashlib.sha256(normalized_prompt.encode("utf-8")).hexdigest(),
        "tools": tool_profile(agent), "implementation": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "environment": {"os": platform.system(), "python": platform.python_version(),
                        "packages": {name: version(name) for name in ("deepagents", "langchain-openai", "langgraph")},
                        "shell": os.environ.get("COMSPEC", "cmd.exe") if os.name == "nt" else "/bin/sh",
                        "permissions": "local_user", "read_only_file_tools": list(read_only_paths)},
    }
    profile["environment"]["memory_enabled"] = memory_enabled
    profile["environment"]["run_config"] = run_config
    return WorkerSession(agent, profile)


def create_agent(workspace: Path, *, model_name: str | None = None, memory_enabled: bool = True,
                 read_only_paths: tuple[str, ...] = (), tools: list | None = None,
                 system_prompt: str | None = None, model_parameters: dict | None = None,
                 memory_layout: MemoryLayout | None = None, run_config: dict | None = None) -> "CompiledStateGraph":
    """Принимает рабочие настройки и tools; возвращает граф той же фабрики, которую использует replay."""
    model = model_name or os.environ.get("OPENROUTER_MODEL", "")
    if not model:
        from command_agent_memory.application_config import read_local_settings

        model = read_local_settings().get("model", "")
    options = {"model": model, "model_parameters": model_parameters or {}, "run_config": run_config or {}}
    if system_prompt is not None:
        options["system_prompt"] = system_prompt
    return create_session(workspace, options, memory_layout, tools=tools,
                          read_only_paths=read_only_paths, memory_enabled=memory_enabled).agent
