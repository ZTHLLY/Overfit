"""Opt-in live/CLI integration with fake providers and temporary artifacts."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from overfit import cli
from overfit.config import EmbedSettings, LLMSettings, Settings
from overfit.evaluation import live
from overfit.evaluation.contracts import TraceWriteError
from overfit.evaluation.trace import TraceWriter
from overfit.models import Chunk
from overfit.query import generator, retriever


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        _env_file=None, llm=LLMSettings(_env_file=None, model="fixture", api_key="SECRET"),
        embed=EmbedSettings(_env_file=None, model="fixture-embed", api_key="SECRET"),
        outputs_dir=tmp_path / "outputs", index_dir=tmp_path / "index", max_retries=0,
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(generator, "get_settings", lambda: settings)
    monkeypatch.setattr(live, "collect_code_identity", lambda _: {"git_head": None, "dirty": None})
    monkeypatch.setattr(live, "read_index_identity", lambda _: {
        "path": "fixture.db", "meta": {}, "documents": [], "snapshot_sha256": None,
    })
    chunks = [Chunk("c1", "Synthetic course text", "notes.pdf", 5)]
    calls = []
    actions = []

    class Store:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            actions.append("closed")

    def open_store(course):
        records = list((settings.outputs_dir / "eval").glob("*/traces.jsonl"))
        assert records and json.loads(records[-1].read_text().splitlines()[0])["event_type"] == "run_started"
        actions.append("opened")
        return Store(), object(), settings

    monkeypatch.setattr(cli, "_open_store", open_store)
    monkeypatch.setattr(retriever, "gather_material", lambda *a, **kw: chunks)
    actual_generator = generator.Generator

    def use_response(response):
        def create(**kwargs):
            calls.append(kwargs)
            if isinstance(response, BaseException):
                raise response
            if not isinstance(response, str):
                return response
            return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=response))])])
        instance = actual_generator(settings.llm)
        instance._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        monkeypatch.setattr(generator, "Generator", lambda *args, **kwargs: instance)

    use_response(json.dumps({"items": [{"topic": "T", "question": "Q", "answer": "A",
                                        "source": "notes.pdf", "page": 5}]}))
    return SimpleNamespace(settings=settings, calls=calls, actions=actions, open_store=open_store,
                           use_response=use_response, chunks=chunks)


def run(setup, **kwargs):
    return live.run_mock_trace(setup.settings, setup.open_store, course="COURSE",
                               questions=1, topic=None, material=0, **kwargs)


def events(path):
    return [json.loads(line) for line in (Path(path) / "traces.jsonl").read_text().splitlines()]


def test_live_commits_complete_run_and_never_overwrites_course_files(setup):
    setup.settings.outputs_dir.mkdir()
    old = setup.settings.output_path("COURSE", "mock_exam.md")
    old.write_text("old exam")
    result = run(setup)
    assert result["status"] == "completed" and result["exit_code"] == 0
    run_dir = Path(result["run_dir"])
    assert old.read_text() == "old exam"
    assert (run_dir / "COURSE_mock_exam.md").exists()
    assert "COURSE_answers.md" in (run_dir / "COURSE_mock_exam.md").read_text()
    log = events(run_dir)
    assert log[-1]["event_type"] == "run_finished"
    assert log[-1]["payload"]["generation_outcome"] == "retained"
    assert log[-1]["payload"]["counts"]["call_count"] == len(setup.calls) == 1
    assert "SECRET" not in (run_dir / "manifest.json").read_text()
    assert setup.actions == ["opened", "closed"]
    second = run(setup)
    assert second["run_dir"] != result["run_dir"]


@pytest.mark.parametrize("raw,outcome", [
    ('{"items": []}', "model_empty"),
    (json.dumps({"items": [{"topic": "T", "question": "Q", "answer": "A",
                           "source": "unknown.pdf", "page": 5}]}), "all_dropped"),
])
def test_empty_and_all_dropped_are_distinct_successes(setup, raw, outcome):
    setup.use_response(raw)
    result = run(setup)
    assert result["status"] == "empty" and result["exit_code"] == 0
    path = Path(result["run_dir"])
    assert not (path / "COURSE_mock_exam.md").exists()
    assert events(path)[-1]["payload"]["generation_outcome"] == outcome
    assert json.loads((path / "result.json").read_text())["generation_outcome"] == outcome


def test_initialization_failure_contacts_no_service(setup, monkeypatch):
    def fail(*args, **kwargs):
        raise TraceWriteError("SECRET failure")
    monkeypatch.setattr(TraceWriter, "create", fail)
    result = run(setup)
    assert result["exit_code"] == 1 and result["run_dir"] is None
    assert setup.actions == [] and setup.calls == []
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("event_name,expected_calls", [("materials_selected", 0), ("call_started", 0), ("call_finished", 1)])
def test_mid_trace_failure_stops_further_calls(setup, monkeypatch, event_name, expected_calls):
    original = TraceWriter.emit
    def fail(self, event_type, payload):
        if event_type == event_name:
            self.failed = True
            raise TraceWriteError("fixture failure")
        return original(self, event_type, payload)
    monkeypatch.setattr(TraceWriter, "emit", fail)
    result = run(setup)
    assert result["exit_code"] == 1 and len(setup.calls) == expected_calls
    assert events(result["run_dir"])[-1]["event_type"] != "run_finished"


@pytest.mark.parametrize("error,status,code", [(RuntimeError("SECRET"), "failed", 1), (KeyboardInterrupt(), "interrupted", 130)])
def test_early_failure_has_no_materials_or_calls(setup, error, status, code):
    def fail(course):
        raise error
    setup.open_store = fail
    result = run(setup)
    assert result["status"] == status and result["exit_code"] == code
    log = events(result["run_dir"])
    assert [e["event_type"] for e in log] == ["run_started", "run_finished"]
    assert log[-1]["payload"]["generation_outcome"] == "not_started"
    assert "SECRET" not in json.dumps(log)
    assert not setup.calls


def test_interrupted_stream_records_partial_then_terminal(setup):
    def stream():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="partial"))])
        raise KeyboardInterrupt()
    setup.use_response(stream())
    result = run(setup)
    assert result["exit_code"] == 130
    log = events(result["run_dir"])
    finished = next(e["payload"] for e in log if e["event_type"] == "call_finished")
    assert finished["content"] == "partial" and finished["response_complete"] is False
    assert log[-1]["payload"]["status"] == "interrupted"
    assert len(setup.calls) == 1


@pytest.mark.parametrize("questions,material", [(0, 0), (-1, 0), (1, -1)])
def test_invalid_trace_arguments_stop_before_any_service(setup, questions, material):
    result = live.run_mock_trace(setup.settings, setup.open_store, course="COURSE",
                                 questions=questions, topic=None, material=material)
    assert result["exit_code"] == 1 and result["run_dir"] is None
    assert setup.calls == [] and setup.actions == []


def test_cli_trace_route_runs_and_default_route_does_not_create_eval(setup, monkeypatch):
    result = CliRunner().invoke(cli.app, ["mock", "--course", "COURSE", "--trace"])
    assert result.exit_code == 0, result.output
    assert "Trace status: completed" in result.output
    eval_root = setup.settings.outputs_dir / "eval"
    count = len(list(eval_root.iterdir()))
    # The original branch is deliberately not subject to the new trace assertion.
    class Store:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
    monkeypatch.setattr(cli, "_open_store", lambda course: (Store(), object(), setup.settings))
    result = CliRunner().invoke(cli.app, ["mock", "--course", "COURSE"])
    assert result.exit_code == 0, result.output
    assert setup.settings.output_path("COURSE", "mock_exam.md").exists()
    assert len(list(eval_root.iterdir())) == count
    assert "timeout" not in setup.calls[-1]


@pytest.mark.parametrize("failed_artifact", [
    "result.json", "report.md", "COURSE_mock_exam.md", "COURSE_answers.md",
])
def test_artifact_write_failure_records_failed_terminal_when_log_writable(
    setup, monkeypatch, failed_artifact,
):
    from overfit.evaluation.replay import inspect_run

    original = TraceWriter._write_atomic
    def fail(self, name, content):
        if name == failed_artifact:
            raise OSError("fixture disk failure")
        return original(self, name, content)
    monkeypatch.setattr(TraceWriter, "_write_atomic", fail)
    result = run(setup)
    assert result["exit_code"] == 1
    assert len(setup.calls) == 1
    terminal = events(result["run_dir"])[-1]
    assert terminal["event_type"] == "run_finished"
    assert terminal["payload"]["status"] == "failed"
    assert terminal["payload"]["stage"] == "artifacts"
    assert failed_artifact not in terminal["payload"]["artifacts"]
    replay = inspect_run(Path(result["run_dir"]))
    assert replay["run_status"] == "failed" and replay["trace_integrity"] == "complete"
    assert replay["generation_outcome"] == "retained"


def test_read_index_identity_uses_document_manifest_not_snapshot(tmp_path):
    path = tmp_path / "test.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        db.execute("CREATE TABLE documents (source TEXT, hash TEXT)")
        db.execute("INSERT INTO meta VALUES ('parser', 'fixture')")
        db.execute("INSERT INTO documents VALUES ('a.pdf', 'digest')")
    identity = live.read_index_identity(path)
    assert identity["meta"] == {"parser": "fixture"}
    assert identity["documents"] == [{"source": "a.pdf", "hash": "digest"}]
    assert identity["snapshot_sha256"] is None
    assert len(identity["document_manifest_sha256"]) == 64
    assert identity == live.read_index_identity(path)


def test_generation_exhaustion_is_failed_not_model_empty(setup):
    setup.use_response(RuntimeError("SECRET provider error"))
    result = run(setup)
    assert result["status"] == "failed" and result["exit_code"] == 1
    assert len(setup.calls) == 3
    terminal = events(result["run_dir"])[-1]["payload"]
    assert terminal["generation_outcome"] == "failed" and terminal["stage"] == "generation"
    assert "SECRET" not in json.dumps(events(result["run_dir"]))


def test_invalid_material_metadata_is_not_silently_repaired(setup):
    setup.chunks[:] = [Chunk("c1", "text", "notes.pdf", 5, page_end=3)]
    result = run(setup)
    assert result["exit_code"] == 1 and not setup.calls
    log = events(result["run_dir"])
    assert [e["event_type"] for e in log] == ["run_started", "run_finished"]
    assert log[-1]["payload"]["stage"] == "selection"


def test_terminal_persistence_error_is_nonzero_even_if_marker_is_readable(setup, monkeypatch):
    original = TraceWriter.emit
    def fail_after_write(self, event_type, payload):
        original(self, event_type, payload)
        if event_type == "run_finished":
            self.failed = True
            raise TraceWriteError("Simulated late fsync confirmation failure")
    monkeypatch.setattr(TraceWriter, "emit", fail_after_write)
    result = run(setup)
    assert result["exit_code"] == 1 and result["status"] == "failed"
    assert events(result["run_dir"])[-1]["payload"]["status"] == "completed"
    assert len(setup.calls) == 1


def test_cli_replay_is_offline_and_does_not_modify_originals(setup, monkeypatch):
    result = run(setup)
    run_dir = Path(result["run_dir"])
    originals = {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}
    def forbidden(*args, **kwargs):
        raise AssertionError("Replay must not load settings, models, or indexes")
    monkeypatch.setattr(cli, "get_settings", forbidden)
    monkeypatch.setattr(cli, "_open_store", forbidden)
    monkeypatch.setattr(generator, "Generator", forbidden)
    monkeypatch.setattr(generator, "get_settings", forbidden)
    monkeypatch.setattr(live, "read_index_identity", forbidden)
    replay = CliRunner().invoke(cli.app, ["eval", "replay", str(run_dir)])
    assert replay.exit_code == 0, replay.output
    assert "completed / complete" in replay.output
    assert {name: (run_dir / name).read_bytes() for name in originals} == originals
    assert len(setup.calls) == 1
    report = json.loads(next((run_dir / "replays").glob("*/result.json")).read_text())
    assert report["semantic_status"] == "not_evaluated"
    assert report["original_cli_exit_status"] is None


def test_cli_replay_incomplete_trace_returns_nonzero(setup):
    result = run(setup)
    run_dir = Path(result["run_dir"])
    log = run_dir / "traces.jsonl"
    log.write_text("\n".join(log.read_text().splitlines()[:-1]) + "\n")
    replay = CliRunner().invoke(cli.app, ["eval", "replay", str(run_dir)])
    assert replay.exit_code == 1
    assert "unknown / incomplete" in replay.output


def test_cli_replay_complete_early_failure_is_not_run_success(setup):
    def failure(course):
        raise RuntimeError("fixture")
    setup.open_store = failure
    result = run(setup)
    replay = CliRunner().invoke(cli.app, ["eval", "replay", result["run_dir"]])
    assert replay.exit_code == 0, replay.output
    assert "failed / complete" in replay.output


@pytest.mark.parametrize("provider_result,status,outcome", [
    (RuntimeError("synthetic exhaustion"), "failed", "failed"),
    (KeyboardInterrupt(), "interrupted", None),
])
def test_terminal_generation_failure_and_interrupt_replay_distinguish_unknown(
    setup, provider_result, status, outcome,
):
    from overfit.evaluation.replay import inspect_run

    setup.use_response(provider_result)
    result = run(setup)
    assert result["status"] == status
    replay = inspect_run(Path(result["run_dir"]))
    assert replay["trace_integrity"] == "complete", replay["problems"]
    assert replay["run_status"] == status
    assert replay["generation_outcome"] == outcome
    terminal = events(result["run_dir"])[-1]["payload"]
    assert terminal["generation_outcome"] == outcome
