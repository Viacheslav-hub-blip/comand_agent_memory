"""Сборка переносимого комплекта Agent Skills из исходников репозитория.

Функции: data_modules выбирает общий код работы с данными, setup_files собирает
файлы настройки, build_skill собирает мост, build_standalone собирает автономные
скиллы, write_bundle записывает ZIP, main разбирает аргументы.
"""
import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def data_modules(repository: Path) -> list[Path]:
    """Принимает корень исходников, возвращает модули данных для переносимых скиллов."""
    package = repository / "command_agent_memory"
    modules = [package / filename for filename in ("__init__.py", "application_config.py", "connections.py",
               "skill_resources.py", "run_reports.py", "worker_agents.py", "memory_replay/__init__.py",
               "memory_replay/replay_scenario.py")]
    for directory in ("history_sources", "memory_storage", "team_memory_delivery"):
        modules.extend((package / directory).glob("*.py"))
    return modules


def setup_files(repository: Path) -> dict[str, Path]:
    """Принимает корень исходников, возвращает файлы отдельного скилла настройки и его runtime."""
    name = "agent-memory-setup"
    skill = repository / ".agents/skills" / name
    if not skill.is_dir():
        return {}
    files = {f"{name}/{path.relative_to(skill).as_posix()}": path for path in skill.rglob("*")
             if path.is_file() and path.suffix in {".md", ".py"} and not path.is_relative_to(skill / "scripts/io_lib")}
    files.update({f"{name}/scripts/io_lib/{path.relative_to(repository).as_posix()}": path
                  for path in data_modules(repository)})
    license_file = repository / "LICENSE"
    if license_file.is_file():
        files[f"{name}/LICENSE"] = license_file
    return files


def build_skill(repository: Path, output: Path) -> Path:
    """Собирает комплект из корня исходников по заданному пути ZIP и возвращает путь."""
    name = "update-agent-memory"
    skill = repository / ".agents" / "skills" / name
    package = repository / "command_agent_memory"
    if not (skill / "SKILL.md").is_file() or not (package / "memory_pipeline.py").is_file():
        raise FileNotFoundError("Для сборки нужны исходники скилла и command_agent_memory.")
    files = {
        f"{name}/{path.relative_to(skill).as_posix()}": path
        for path in skill.rglob("*")
        if path.is_file() and path.suffix in {".md", ".py"} and not path.is_relative_to(skill / "scripts/runtime")
    }
    files.update({
        f"{name}/scripts/runtime/{path.relative_to(repository).as_posix()}": path
        for path in package.rglob("*")
        if path.is_file() and path.suffix in {".md", ".py"}
    })
    license_file = repository / "LICENSE"
    if license_file.is_file():
        files[f"{name}/LICENSE"] = license_file
    files.update(setup_files(repository))
    return write_bundle(files, output)


def build_standalone(repository: Path, output: Path) -> Path:
    """Принимает корень исходников и путь ZIP; возвращает архив скиллов без модельного конвейера."""
    name = "agent-memory-cycle"
    router = repository / ".agents/skills" / name
    package = repository / "command_agent_memory"
    files = {f"{name}/{p.relative_to(router).as_posix()}": p for p in router.rglob("*")
             if p.is_file() and p.suffix in {".md", ".py"} and not p.is_relative_to(router / "scripts/io_lib")}
    files.update({p.relative_to(package / "skills").as_posix(): p for p in (package / "skills").rglob("*.md")})
    files.update({f"{name}/scripts/io_lib/{p.relative_to(repository).as_posix()}": p
                  for p in data_modules(repository)})
    files[f"{name}/LICENSE"] = repository / "LICENSE"
    files.update(setup_files(repository))
    return write_bundle(files, output)


def write_bundle(files: dict[str, Path], output: Path) -> Path:
    """Принимает имена и исходники файлов; записывает воспроизводимый ZIP по новому пути."""
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x") as archive:
        for filename, path in sorted(files.items()):
            entry = ZipInfo(filename)
            entry.create_system = 3
            archive.writestr(entry, path.read_bytes().replace(b"\r\n", b"\n"), compress_type=ZIP_DEFLATED)
    return output


def main() -> None:
    """Без аргументов функции читает CLI и печатает путь к собранному архиву."""
    parser = argparse.ArgumentParser(description="Сборка переносимого скилла update-agent-memory.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--standalone", action="store_true", help="Собрать автономные скиллы без конвейера.")
    args = parser.parse_args()
    builder = build_standalone if args.standalone else build_skill
    print(builder(Path(__file__).resolve().parents[1], args.output))


if __name__ == "__main__":
    main()
