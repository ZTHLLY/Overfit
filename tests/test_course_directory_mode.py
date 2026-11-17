"""Course-directory mode: `cd` into a course folder and run one command."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from overfit import cli, config
from overfit.config import EmbedSettings, LLMSettings, Settings
from overfit.errors import OverfitError
from overfit.ingestion import loader

_MODEL_ENV = (
    "EMBED_BASE_URL", "EMBED_API_KEY", "EMBED_MODEL",
    "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
    "COURSES_DIR", "OUTPUTS_DIR", "INDEX_DIR", "EXTENSIONS",
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in _MODEL_ENV:
        monkeypatch.delenv(name, raising=False)


# ---- configuration layering ------------------------------------------------


def test_global_env_file_follows_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.global_env_file() == tmp_path / "overfit" / ".env"


def test_global_env_file_defaults_to_home_config(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert config.global_env_file() == Path.home() / ".config" / "overfit" / ".env"


def test_local_env_overrides_global_env(clean_env, tmp_path):
    global_file = tmp_path / "global.env"
    local_file = tmp_path / "local.env"
    global_file.write_text("LLM_MODEL=from-global\nLLM_API_KEY=global-key\n")
    local_file.write_text("LLM_MODEL=from-local\n")

    loaded = LLMSettings(_env_file=(global_file, local_file))

    assert loaded.model == "from-local"
    assert loaded.api_key == "global-key"


def test_settings_read_global_then_working_directory_env(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")
    assert config.env_files() == (Path("/xdg/overfit/.env"), Path(".env"))


def test_generation_defaults_to_hosted_deepseek(clean_env):
    embed = EmbedSettings(_env_file=None)
    llm = LLMSettings(_env_file=None)

    # Embedding stays on local Ollama BGE-M3 for this phase.
    assert embed.base_url == "http://localhost:11434/v1"
    assert embed.model == "bge-m3"
    assert llm.base_url == "https://api.deepseek.com"
    assert llm.model == "deepseek-v4-flash"
    assert llm.api_key == ""


def test_missing_embedding_key_fails_before_client_construction(clean_env):
    from overfit.embedding import Embedder

    embedder = Embedder(EmbedSettings(_env_file=None, api_key=""))
    with pytest.raises(OverfitError, match="EMBED_API_KEY"):
        embedder._ensure_client()


def test_missing_generation_key_fails_before_client_construction(clean_env):
    from overfit.query.generator import Generator

    generator = Generator(LLMSettings(_env_file=None))
    with pytest.raises(OverfitError, match="LLM_API_KEY"):
        generator._ensure_client()


# ---- course-directory paths ------------------------------------------------


def test_course_name_comes_from_directory_name(tmp_path):
    folder = tmp_path / "IFN580 Machine Learning"
    folder.mkdir()
    assert config.course_name_for(folder) == "IFN580_Machine_Learning"


def test_course_directory_settings_keep_everything_inside_the_folder(clean_env, tmp_path):
    folder = tmp_path / "IFN580 ML"
    folder.mkdir()
    base = Settings(_env_file=None)

    here = base.for_course_directory(folder)
    course = config.course_name_for(folder)

    assert here.course_dir(course) == folder.resolve()
    assert here.db_path(course) == folder.resolve() / ".overfit" / "index" / f"{course}.db"
    assert here.outputs_dir == folder.resolve() / ".overfit"
    assert here.output_path(course, "mock_exam.md") == folder.resolve() / f"{course}_mock_exam.md"
    assert here.output_path(course, "answers.md") == folder.resolve() / f"{course}_answers.md"
    # Model configuration is inherited unchanged.
    assert here.llm == base.llm and here.embed == base.embed


def test_generated_files_are_listed_for_exclusion(clean_env, tmp_path):
    here = Settings(_env_file=None).for_course_directory(tmp_path)
    course = config.course_name_for(tmp_path)
    assert here.generated_files(course) == frozenset({
        tmp_path.resolve() / f"{course}_mock_exam.md",
        tmp_path.resolve() / f"{course}_answers.md",
    })


def test_loader_skips_excluded_files(tmp_path):
    (tmp_path / "notes.md").write_text("real notes")
    (tmp_path / "C_mock_exam.md").write_text("generated")
    excluded = frozenset({(tmp_path / "C_mock_exam.md").resolve()})

    found = loader.find_documents(tmp_path, (".md",), exclude=excluded)
    ignored = loader.count_ignored(tmp_path, (".pdf",), exclude=excluded)

    assert [path.name for path in found] == ["notes.md"]
    assert ignored == {".md": 1}


# ---- CLI -------------------------------------------------------------------


def test_bare_command_prints_help():
    result = CliRunner().invoke(cli.app, [])
    assert result.exit_code == 0
    assert "hello overfit" not in result.output
    assert "mock" in result.output and "Usage" in result.output


def _isolated_settings(monkeypatch, tmp_path):
    """Real settings resolution, but no user .env files and no network."""
    for name in _MODEL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "env_files", lambda: ())
    monkeypatch.setattr(cli, "get_settings", config.get_settings)
    config.get_settings.cache_clear()


def test_mock_without_course_ingests_then_generates_from_cwd(monkeypatch, tmp_path):
    folder = tmp_path / "IFN580"
    folder.mkdir()
    monkeypatch.chdir(folder)
    _isolated_settings(monkeypatch, tmp_path)
    calls: dict = {}

    def fake_ingest(course, directory, settings, **kwargs):
        calls["ingest"] = (course, directory, settings)

    def fake_trace(settings, open_store, **kwargs):
        calls["trace"] = (settings, kwargs)
        calls["open_store_settings"] = cli.get_settings()
        return {"error": None, "status": "failed", "run_dir": None, "exit_code": 1}

    monkeypatch.setattr(cli, "_run_ingest", fake_ingest)
    monkeypatch.setattr("overfit.evaluation.live.run_mock_trace", fake_trace)

    result = CliRunner().invoke(cli.app, ["mock", "--questions", "5"])

    assert result.exit_code == 1, result.output
    course, directory, ingest_settings = calls["ingest"]
    assert course == "IFN580"
    assert directory == folder.resolve()
    assert ingest_settings.db_path("IFN580") == folder.resolve() / ".overfit" / "index" / "IFN580.db"
    trace_settings, kwargs = calls["trace"]
    assert kwargs["course"] == "IFN580"
    assert trace_settings.outputs_dir == folder.resolve() / ".overfit"
    # Everything that calls get_settings() later (store, generator) sees the same paths.
    assert calls["open_store_settings"].outputs_dir == folder.resolve() / ".overfit"


def test_mock_with_course_keeps_explicit_workflow(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _isolated_settings(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr(cli, "_run_ingest", lambda *a, **k: called.append(a))
    monkeypatch.setattr(
        "overfit.evaluation.live.run_mock_trace",
        lambda settings, open_store, **kwargs: {
            "error": None, "status": "failed", "run_dir": None, "exit_code": 1,
        },
    )

    CliRunner().invoke(cli.app, ["mock", "--course", "IFN580", "--questions", "5"])

    assert called == []


def test_status_without_course_reads_cwd_index(monkeypatch, tmp_path):
    folder = tmp_path / "ML"
    folder.mkdir()
    monkeypatch.chdir(folder)
    _isolated_settings(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli.app, ["status"])

    assert result.exit_code == 1
    assert str(folder.resolve() / ".overfit" / "index" / "ML.db") in result.output.replace("\n", "")


# ---- review hardening ------------------------------------------------------


def test_course_directory_mode_ignores_local_env(monkeypatch, tmp_path):
    """A downloaded course folder must not redirect the user's global keys."""
    xdg = tmp_path / "xdg"
    (xdg / "overfit").mkdir(parents=True)
    (xdg / "overfit" / ".env").write_text(
        "EMBED_API_KEY=real-key\nEMBED_BASE_URL=https://openrouter.ai/api/v1\n"
    )
    folder = tmp_path / "Downloaded"
    folder.mkdir()
    (folder / ".env").write_text("EMBED_BASE_URL=https://attacker.example/v1\n")
    for name in _MODEL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.chdir(folder)
    config.get_settings.cache_clear()

    cli._resolve_course(None)
    settings = config.get_settings()

    assert settings.embed.base_url == "https://openrouter.ai/api/v1"
    assert settings.embed.api_key == "real-key"


def test_explicit_course_still_reads_working_directory_env(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("LLM_MODEL=from-repo-env\n")
    for name in _MODEL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    config.get_settings.cache_clear()

    cli._resolve_course("IFN580")

    assert config.get_settings().llm.model == "from-repo-env"


def test_explicit_course_clears_an_earlier_course_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _isolated_settings(monkeypatch, tmp_path)
    cli._resolve_course(None)
    assert config.get_settings().course_root is not None

    cli._resolve_course("IFN580")

    assert config.get_settings().course_root is None


@pytest.mark.parametrize("where", ["home", "root"])
def test_refuses_home_and_filesystem_root(monkeypatch, tmp_path, where):
    _isolated_settings(monkeypatch, tmp_path)
    target = Path.home() if where == "home" else Path("/")
    monkeypatch.chdir(target)

    result = CliRunner().invoke(cli.app, ["mock", "-q", "3"])

    assert result.exit_code == 2
    assert "course folder" in result.output
    assert not (target / ".overfit").exists() or where == "home"


def test_empty_values_do_not_override_earlier_layers(clean_env, tmp_path):
    global_file = tmp_path / "global.env"
    local_file = tmp_path / "local.env"
    global_file.write_text("LLM_API_KEY=global-key\n")
    local_file.write_text("LLM_API_KEY=\n")

    assert LLMSettings(_env_file=(global_file, local_file)).api_key == "global-key"


def test_relative_xdg_config_home_is_ignored(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    assert config.global_env_file() == Path.home() / ".config" / "overfit" / ".env"


def test_judge_settings_accept_the_layered_env_files(monkeypatch, tmp_path):
    from overfit.evaluation.judge import JudgeSettings

    (tmp_path / "overfit").mkdir()
    (tmp_path / "overfit" / ".env").write_text(
        "JUDGE_BASE_URL=https://openrouter.ai/api/v1\nJUDGE_API_KEY=k\nJUDGE_MODEL=m\n"
    )
    for name in ("JUDGE_BASE_URL", "JUDGE_API_KEY", "JUDGE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert JudgeSettings(_env_file=config.env_files()).model == "m"


def test_stale_exam_files_from_a_renamed_folder_are_excluded(clean_env, tmp_path):
    (tmp_path / "OLD_mock_exam.md").write_text("# OLD — Practice Exam\n\n...")
    (tmp_path / "OLD_answers.md").write_text("# OLD — Practice Exam Answers\n\n...")
    (tmp_path / "week5_answers.md").write_text("# Week 5 tutorial solutions\n")
    (tmp_path / "week1").mkdir()
    (tmp_path / "week1" / "lab_answers.md").write_text("real material")
    here = Settings(_env_file=None).for_course_directory(tmp_path)

    excluded = here.generated_files("NEW")
    found = loader.find_documents(tmp_path, (".md",), exclude=excluded)

    assert [p.relative_to(tmp_path).as_posix() for p in found] == [
        "week1/lab_answers.md", "week5_answers.md",
    ]


def test_refuses_an_overfit_workspace_root(monkeypatch, tmp_path):
    (tmp_path / "courses").mkdir()
    monkeypatch.chdir(tmp_path)
    _isolated_settings(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli.app, ["ingest"])

    assert result.exit_code == 2
    assert "course folder" in result.output


def test_reingest_of_existing_index_does_not_probe_the_embedder(monkeypatch, tmp_path):
    from overfit.storage.store import IndexProfile, VectorStore

    folder = tmp_path / "C"
    folder.mkdir()
    monkeypatch.chdir(folder)
    _isolated_settings(monkeypatch, tmp_path)
    cli._resolve_course(None)
    settings = config.get_settings()
    profile = IndexProfile(
        embed_model=settings.embed.model, embed_dim=8,
        chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap,
        parser=settings.pdf_backend,
    )
    VectorStore.open(settings.db_path("C"), profile, course="C", create=True).close()

    class NoProbe:
        model = settings.embed.model

        @property
        def dimension(self):
            raise AssertionError("existing index must not trigger a live probe")

    monkeypatch.setattr("overfit.embedding.get_embedder", NoProbe)

    store, _, _ = cli._open_store("C", create=True)
    store.close()
