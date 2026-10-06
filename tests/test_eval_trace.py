"""Fault-injected trace tests: no settings, network or model requests."""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from overfit.evaluation import trace
from overfit.evaluation.contracts import Event, Manifest, TraceWriteError
from overfit.evaluation.trace import TraceWriter, safe_endpoint, safe_error

TASK = {"course": "IFN580", "topic": None, "questions": 2, "material_budget": 4}


def make_writer(tmp_path):
    return TraceWriter.create(tmp_path, TASK, {"generation_model": "fake"})


def read_events(writer):
    return [
        json.loads(line)
        for line in (writer.run_dir / "traces.jsonl").read_text().splitlines()
    ]


def test_create_records_identity_first_and_file_permissions(tmp_path):
    writer = make_writer(tmp_path)
    assert writer.run_dir.parent == tmp_path / "eval"
    assert read_events(writer) == writer.events
    assert writer.events[0]["event_type"] == "run_started"
    assert writer.events[0]["seq"] == 1
    assert writer.manifest["task_id"] == writer.manifest["task"]["task_id"]
    assert Manifest.model_validate(writer.manifest).run_id == writer.run_id
    assert writer.run_dir.stat().st_mode & 0o777 == 0o700
    for name in ("manifest.json", "traces.jsonl"):
        assert (writer.run_dir / name).stat().st_mode & 0o777 == 0o600


def test_ids_are_unique_collision_retries_and_task_identity_stable(
    tmp_path, monkeypatch
):
    first = make_writer(tmp_path)
    ids = iter([first.run_id, "another-unique-id"])
    monkeypatch.setattr(trace, "uuid4", lambda: next(ids))
    second = make_writer(tmp_path)
    assert second.run_id == "another-unique-id"
    assert first.manifest["task_id"] == second.manifest["task_id"]
    assert read_events(first)[0]["run_id"] == first.run_id


@pytest.mark.parametrize(
    "changes", [{"questions": 0}, {"questions": True}, {"material_budget": -1}]
)
def test_bad_parameters_fail_before_run_directory(tmp_path, changes):
    with pytest.raises(TraceWriteError):
        TraceWriter.create(tmp_path, TASK | changes, {})
    assert not (tmp_path / "eval").exists()


def test_initial_artifact_failure_leaves_no_start(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("SECRET initial disk failure")

    monkeypatch.setattr(trace.os, "replace", fail)
    with pytest.raises(TraceWriteError) as error:
        make_writer(tmp_path)
    assert "SECRET" not in str(error.value)
    assert not list(tmp_path.rglob("traces.jsonl"))
    assert not list(tmp_path.rglob(".pending-*"))


def test_initial_event_failure_returns_no_writer(tmp_path, monkeypatch):
    original = trace.os.open

    def fail_log(path, flags, *args, **kwargs):
        if Path(path).name == "traces.jsonl":
            raise OSError("SECRET")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(trace.os, "open", fail_log)
    with pytest.raises(TraceWriteError):
        make_writer(tmp_path)
    assert list(tmp_path.rglob("manifest.json"))
    assert not list(tmp_path.rglob("traces.jsonl"))


def test_snapshot_and_each_fallback_call_identity(tmp_path):
    writer = make_writer(tmp_path)
    messages = [{"role": "user", "content": "Actual input"}]
    ids = []
    for index, fmt in enumerate(
        [{"type": "json_schema"}, {"type": "json_object"}, None], 1
    ):
        request = {"messages": messages, "response_format": fmt, "model": "fake"}
        ids.append(writer.start_call(1, index, request))
    messages[0]["content"] = "Later mutation"
    assert len(set(ids)) == 3
    assert writer.last_call_id == ids[-1]
    assert [e["seq"] for e in writer.events] == [1, 2, 3, 4]
    assert all(
        e["payload"]["request"]["messages"][0]["content"] == "Actual input"
        for e in writer.events[1:]
    )


def test_append_failure_preserves_prefix_and_disables_writer(tmp_path, monkeypatch):
    writer = make_writer(tmp_path)
    before = (writer.run_dir / "traces.jsonl").read_bytes()
    original = trace.os.open

    def fail_log(path, flags, *args, **kwargs):
        if Path(path).name == "traces.jsonl":
            raise OSError("SECRET")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(trace.os, "open", fail_log)
    with pytest.raises(TraceWriteError):
        writer.start_call(1, 1, {"model": "fake"})
    assert writer.failed
    assert writer.last_call_id is None
    assert (writer.run_dir / "traces.jsonl").read_bytes() == before
    with pytest.raises(TraceWriteError):
        writer.emit("run_finished", {})


def test_validation_failure_disables_writer_without_corrupting_log(tmp_path):
    writer = make_writer(tmp_path)
    with pytest.raises(TraceWriteError):
        writer.emit("unknown_event", {})
    assert writer.failed
    assert len(read_events(writer)) == 1


def test_non_json_data_is_not_lossily_serialized(tmp_path):
    writer = make_writer(tmp_path)
    with pytest.raises(TraceWriteError):
        writer.emit("materials_selected", {"not_json": object()})
    assert writer.failed
    assert len(writer.events) == 1


def test_artifact_failure_does_not_commit_and_preserves_successful_records(
    tmp_path, monkeypatch
):
    writer = make_writer(tmp_path)

    def fail(*args):
        raise OSError("SECRET artifact failure")

    monkeypatch.setattr(trace.os, "replace", fail)
    with pytest.raises(TraceWriteError):
        writer.write_artifact("result.json", "{}\n")
    assert writer.artifact_failed and not writer.failed
    assert not (writer.run_dir / "result.json").exists()
    assert not list(writer.run_dir.glob(".pending-*"))
    assert len(read_events(writer)) == 1
    with pytest.raises(TraceWriteError):
        writer.start_call(1, 1, {"model": "fake"})
    with pytest.raises(TraceWriteError):
        writer.emit("materials_selected", {"chunks": []})
    with pytest.raises(TraceWriteError):
        writer.write_artifact("another.md", "forbidden")
    with pytest.raises(TraceWriteError):
        writer.finish("completed", "artifacts", "retained")
    # os.replace still fails, but append-only event recording remains usable.
    writer.finish("failed", "artifacts", "retained", safe_error(OSError("SECRET")))
    assert writer.finished and not writer.failed and writer.artifact_failed
    end = read_events(writer)[-1]
    assert end["payload"]["status"] == "failed"
    assert end["payload"]["stage"] == "artifacts"
    assert "result.json" not in end["payload"]["artifacts"]
    with pytest.raises(TraceWriteError):
        writer.finish("failed", "artifacts", "retained")


@pytest.mark.parametrize(
    "name", ["../escape", "/tmp/escape", "x/y", "x\\y", "traces.jsonl", "manifest.json"]
)
def test_artifacts_cannot_escape_or_replace_primary_records(tmp_path, name):
    writer = make_writer(tmp_path)
    with pytest.raises(TraceWriteError):
        writer.write_artifact(name, "bad")
    assert writer.artifact_failed and not writer.failed


def test_artifact_failure_does_not_clear_later_event_failure(tmp_path, monkeypatch):
    writer = make_writer(tmp_path)

    def fail(*args):
        raise OSError("injected")

    monkeypatch.setattr(trace.os, "replace", fail)
    with pytest.raises(TraceWriteError):
        writer.write_artifact("report.md", "report")
    monkeypatch.setattr(trace.os, "fsync", fail)
    with pytest.raises(TraceWriteError):
        writer.finish("failed", "artifacts", "retained")
    assert writer.failed and writer.artifact_failed and not writer.finished
    with pytest.raises(TraceWriteError):
        writer.finish("interrupted", "artifacts", "retained")


def test_interrupted_artifact_write_allows_interrupted_terminal(tmp_path, monkeypatch):
    writer = make_writer(tmp_path)

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(trace.os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        writer.write_artifact("report.md", "report")
    assert writer.artifact_failed and not writer.failed
    writer.finish(
        "interrupted", "artifacts", "retained", safe_error(KeyboardInterrupt())
    )
    assert read_events(writer)[-1]["payload"]["status"] == "interrupted"


def test_artifact_symlink_is_rejected(tmp_path):
    writer = make_writer(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    (writer.run_dir / "report.md").symlink_to(outside)
    with pytest.raises(TraceWriteError):
        writer.write_artifact("report.md", "changed")
    assert outside.read_text() == "untouched"


def test_failed_early_run_can_commit_without_material_or_derived_artifacts(tmp_path):
    writer = make_writer(tmp_path)
    writer.finish("failed", "index", "not_started", safe_error(OSError("SECRET")))
    end = read_events(writer)[-1]
    assert end["payload"]["status"] == "failed"
    assert end["payload"]["artifacts"] == writer.artifacts
    assert set(writer.artifacts) == {"manifest.json"}
    assert writer.finished
    with pytest.raises(TraceWriteError):
        writer.finish("failed", "index", "not_started")


def test_empty_run_requires_reports_and_hashes_do_not_include_log(tmp_path):
    writer = make_writer(tmp_path)
    writer.write_artifact("result.json", '{"generation_outcome":"model_empty"}\n')
    writer.write_artifact("report.md", "Semantics not evaluated.\n")
    writer.finish("empty", "generation", "model_empty")
    end = writer.events[-1]["payload"]
    assert "traces.jsonl" not in end["artifacts"]
    for name, digest in end["artifacts"].items():
        assert (
            hashlib.sha256((writer.run_dir / name).read_bytes()).hexdigest() == digest
        )
    assert writer.finished
    with pytest.raises(TraceWriteError):
        writer.write_artifact("after.md", "forbidden")


def test_completed_requires_exam_pair(tmp_path):
    writer = make_writer(tmp_path)
    writer.write_artifact("result.json", "{}")
    writer.write_artifact("report.md", "report")
    with pytest.raises(TraceWriteError):
        writer.finish("completed", "generation", "retained")
    assert writer.artifact_failed and not writer.failed
    assert not any(e["event_type"] == "run_finished" for e in read_events(writer))


def test_start_without_finish_has_unknown_exact_call_count(tmp_path):
    writer = make_writer(tmp_path)
    writer.start_call(1, 1, {"model": "fake"})
    # No fake create call happened after durable intent.
    writer.finish("interrupted", "generation", None)
    assert writer.events[-1]["payload"]["counts"] == {
        "call_started": 1,
        "call_finished": 0,
        "unresolved": 1,
        "call_count": None,
    }


def test_fsync_failure_can_leave_a_readable_terminal_line(tmp_path, monkeypatch):
    writer = make_writer(tmp_path)

    def fail(fd):
        raise OSError("flush confirmation failed")

    monkeypatch.setattr(trace.os, "fsync", fail)
    with pytest.raises(TraceWriteError):
        writer.finish("failed", "index", "not_started")
    assert writer.failed and not writer.finished
    assert len(writer.events) == 1  # Not acknowledged in memory.
    assert (
        read_events(writer)[-1]["event_type"] == "run_finished"
    )  # But bytes may exist.


def test_interrupt_during_log_persistence_disables_further_appends(
    tmp_path, monkeypatch
):
    writer = make_writer(tmp_path)

    def interrupt(fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(trace.os, "fsync", interrupt)
    with pytest.raises(KeyboardInterrupt):
        writer.emit("materials_selected", {"chunks": []})
    assert writer.failed
    assert len(writer.events) == 1
    with pytest.raises(TraceWriteError):
        writer.finish("interrupted", "selection", "not_started")


def test_tail_damage_does_not_destroy_valid_prefix(tmp_path):
    writer = make_writer(tmp_path)
    with (writer.run_dir / "traces.jsonl").open("ab") as stream:
        stream.write(b'{"schema_version":')
    lines = (writer.run_dir / "traces.jsonl").read_bytes().splitlines()
    assert Event.model_validate_json(lines[0]).event_type == "run_started"
    with pytest.raises(ValidationError):
        Event.model_validate_json(lines[1])


def test_configuration_and_error_secrets_not_written(tmp_path):
    config = {
        "api_key": "SECRET",
        "Authorization": "SECRET",
        "llm": {
            "model": "fake",
            "base_url": "https://alice:SECRET@example.com/v1?key=SECRET#SECRET",
            "api_key": "SECRET",
        },
        "index_profile": {"parser": "pypdf", "api_key": "SECRET"},
    }
    writer = TraceWriter.create(tmp_path, TASK, config)
    writer.finish("failed", "provider", "failed", safe_error(RuntimeError("SECRET")))
    assert "SECRET" not in "".join(p.read_text() for p in writer.run_dir.iterdir())
    assert writer.manifest["config"]["llm"]["base_url"] == "https://example.com/v1"
    assert "api_key" not in writer.manifest["config"]["index_profile"]


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://u:p@host:443/v1?a=b#c", "https://host:443/v1"),
        ("http://[::1]:11434/v1?secret=yes", "http://[::1]:11434/v1"),
        ("not a URL SECRET", "unknown"),
    ],
)
def test_safe_endpoint(url, expected):
    assert safe_endpoint(url) == expected


@pytest.mark.parametrize("value", [None, 1, 1.5, True, b"https://secret@host", [], {}])
def test_safe_endpoint_rejects_non_string_without_attribute_error(value):
    assert safe_endpoint(value) == "unknown"


def test_unknown_schema_is_rejected():
    with pytest.raises(ValidationError):
        Event(
            schema_version=2,
            run_id="run",
            seq=1,
            event_type="run_started",
            time="now",
            payload={},
        )


@pytest.mark.parametrize("version", [True, 1.0, "1", 0, 2])
def test_schema_version_does_not_coerce_boolean_or_numeric_types(version):
    with pytest.raises(ValidationError):
        Event(
            schema_version=version,
            run_id="r",
            seq=1,
            event_type="run_started",
            time="2026-10-05T00:00:00Z",
            payload={},
        )


@pytest.mark.parametrize(
    "timestamp", ["now", "2026-10-05T00:00:00", "2026-10-05T00:00:00+10:00"]
)
def test_timestamp_must_be_explicit_utc(timestamp):
    with pytest.raises(ValidationError):
        Event(run_id="r", seq=1, event_type="run_started", time=timestamp, payload={})


def test_code_identity_reads_allowlist_not_env(tmp_path):
    (tmp_path / "src" / "overfit").mkdir(parents=True)
    (tmp_path / "src" / "overfit" / "one.py").write_text("pass\n")
    (tmp_path / "uv.lock").write_text("lock")
    (tmp_path / ".env").write_text("SECRET")
    # Synthetic fixture only; never reads the repository's real .env.
    identity = trace.collect_code_identity(tmp_path)
    assert set(identity["files"]) == {"src/overfit/one.py", "uv.lock"}
    assert "SECRET" not in json.dumps(identity)
