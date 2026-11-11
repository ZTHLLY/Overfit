"""Discover course IDs from configured paths without opening index databases."""

from pathlib import Path

from overfit.config import Settings


def _entries(root: Path) -> list[Path]:
    try:
        return list(root.iterdir())
    except FileNotFoundError:
        return []


def _exact_course_id(settings: Settings, name: str) -> bool:
    if name.startswith("."):
        return False
    try:
        # Reuse the --course path mapping; do not advertise an ID that would
        # be normalized to a different course when passed to a command.
        return settings.course_dir(name).name == name
    except ValueError:
        return False


def discover_courses(settings: Settings) -> list[tuple[str, bool]]:
    """Return sorted (course ID, index file present) pairs from top-level entries."""
    indexed = {
        path.stem for path in _entries(settings.index_dir)
        if path.suffix == ".db" and _exact_course_id(settings, path.stem) and path.is_file()
    }
    sources = {
        path.name for path in _entries(settings.courses_dir)
        if _exact_course_id(settings, path.name) and path.is_dir()
    }
    return [(course, course in indexed) for course in sorted(indexed | sources)]
