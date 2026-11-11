"""Course discovery is a local, read-only CLI operation."""

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from overfit import cli
from overfit.config import RerankSettings, Settings, get_settings


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INDEX_DIR", "custom-indexes")
    monkeypatch.setenv("COURSES_DIR", "custom-courses")
    monkeypatch.setenv("OUTPUTS_DIR", "custom-outputs")
    get_settings.cache_clear()

    def forbidden(*args, **kwargs):
        pytest.fail("Listing courses must not open indexes, prepare providers, or create directories")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(cli, "_open_store", forbidden)
    monkeypatch.setattr(Settings, "ensure_dirs", forbidden)
    monkeypatch.setattr(RerankSettings, "for_query", forbidden)
    yield tmp_path / "custom-indexes", tmp_path / "custom-courses", tmp_path / "custom-outputs"
    get_settings.cache_clear()


def invoke():
    return CliRunner().invoke(cli.app, ["course", "list"])


def test_empty_catalog_does_not_create_missing_directories(catalog):
    result = invoke()
    assert result.exit_code == 0, result.output
    assert "No courses found" in result.output
    assert not any(path.exists() for path in catalog)


def test_catalog_merges_sorted_exact_ids_and_only_checks_presence(catalog):
    indexes, sources, outputs = catalog
    indexes.mkdir()
    sources.mkdir()
    # Deliberately not SQLite: a file's presence does not validate the index.
    for name in ("CS-2.db", "CS_0.db", "CS-2.db-wal", "CS-2.db-shm", ".hidden.db", "space name.db", "wrong.DB"):
        (indexes / name).write_text("not a database")
    (indexes / "not-a-file.db").mkdir()
    for name in ("CS.3", "CS-2", ".hidden", "space name", "trailing ", "中文"):
        (sources / name).mkdir()
    (sources / "CS.3" / "not-a-top-level-course").mkdir()
    (sources / "not-a-directory").touch()
    # The defaults must not leak into a catalog configured for other directories.
    Path("index").mkdir()
    Path("index/IGNORED.db").touch()
    Path("courses/IGNORED").mkdir(parents=True)

    result = invoke()
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "CS-2\tindex present", "CS.3\tnot indexed", "CS_0\tindex present",
    ]
    assert not outputs.exists()


def test_unreadable_catalog_fails_without_exposing_exception(catalog, monkeypatch):
    indexes, _, _ = catalog
    original = Path.iterdir

    def iterdir(path):
        if path == indexes:
            raise PermissionError("do-not-print-internal-path")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    result = invoke()
    assert result.exit_code == 1
    assert "COURSES_DIR" in result.output and "INDEX_DIR" in result.output
    assert "do-not-print-internal-path" not in result.output


def test_unresolvable_course_directory_reports_configuration_error(catalog, monkeypatch):
    monkeypatch.setenv("COURSES_DIR", "~overfit_test_nonexistent_home")
    result = invoke()
    assert result.exit_code == 1
    assert "check COURSES_DIR and INDEX_DIR" in result.output
    assert isinstance(result.exception, SystemExit)
