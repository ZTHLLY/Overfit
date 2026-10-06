"""Offline replay uses persisted facts, validates prefixes, and preserves originals."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from overfit.evaluation.citations import check_citation
from overfit.evaluation.contracts import TraceReadError, TraceWriteError
from overfit.evaluation.replay import inspect_run, replay_run
from overfit.evaluation.report import render_report, summarize
from overfit.evaluation.trace import TraceWriter


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def fixture_run(tmp_path, *, empty=False, action="keep", terminal=True):
    writer = TraceWriter.create(tmp_path, {"course": "FIXTURE", "questions": 1,
                                         "topic": None, "material_budget": 1}, {})
    chunks = [{"id": "c1", "source": "notes.pdf", "page": 5, "page_end": 5,
               "section": None, "text": "Synthetic evidence"}]
    writer.emit("materials_selected", {"chunks": chunks, "materials_sha256": digest(chunks),
                "selection": {"actual_count": 1}, "index": {}})
    call = writer.start_call(1, 1, {"model": "fixture", "stream": True,
                "temperature": 0.2, "timeout": 30, "response_format": None,
                "messages": [{"role": "user", "content": "Synthetic prompt"}]})
    before = {"topic": "T", "question": "Q", "answer": "A", "source": "notes.pdf",
              "page": {"keep": 5, "repair": 4, "drop": 20}[action]}
    raw = json.dumps({"items": [] if empty else [before]})
    writer.emit("call_finished", {"call_id": call, "status": "completed", "content": raw,
                "response_complete": True, "elapsed_seconds": 0.1, "error": None, "usage": None})
    writer.emit("validation_finished", {"call_id": call, "status": "valid",
                                        "item_count": 0 if empty else 1, "error": None})
    if not empty:
        after = None if action == "drop" else {**before, "page": 5}
        writer.emit("citation_processed", {"call_id": call, "item_id": f"{call}-item-1",
                    "original_index": 1, "final_index": None if after is None else 1,
                    "pre_policy": before, "post_policy": after, "action": action,
                    "reason": "fixture", "pre_check": check_citation(before, chunks),
                    "post_check": check_citation(after, chunks) if after else None})
    result = summarize(writer.manifest, writer.events)
    writer.write_artifact("result.json", json.dumps(result))
    writer.write_artifact("report.md", render_report(result))
    status = "empty" if empty or action == "drop" else "completed"
    if status == "completed":
        writer.write_artifact("FIXTURE_mock_exam.md", "Synthetic exam")
        writer.write_artifact("FIXTURE_answers.md", "Synthetic answers")
    if terminal:
        writer.finish(status, "complete", result["generation_outcome"])
    return writer


def rewrite(writer, events):
    (writer.run_dir / "traces.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events))


@pytest.mark.parametrize("empty,action,outcome,rate", [
    (False, "keep", "retained", 1), (False, "repair", "retained", 0),
    (False, "drop", "all_dropped", 0), (True, "keep", "model_empty", None),
])
def test_complete_replay_recomputes_and_never_changes_originals(tmp_path, empty, action, outcome, rate):
    writer = fixture_run(tmp_path, empty=empty, action=action)
    files = {p.name: p.read_bytes() for p in writer.run_dir.iterdir()}
    first, second = replay_run(writer.run_dir), replay_run(writer.run_dir)
    assert first["replay_dir"] != second["replay_dir"]
    assert first["trace_integrity"] == "complete"
    assert first["generation_outcome"] == outcome
    assert first["pre_citation_rate"] == rate
    assert first["semantic_status"] == "not_evaluated"
    assert first["original_cli_exit_status"] is None
    assert first["call_count"] == 1
    assert files == {n: (writer.run_dir / n).read_bytes() for n in files}
    assert Path(first["replay_dir"], "report.md").exists()


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_complete_early_failure_without_materials(tmp_path, status):
    writer = TraceWriter.create(tmp_path, {"course": "F", "questions": 1}, {})
    writer.finish(status, "index", "not_started", {"type": "io_error", "message": "failure"})
    result = inspect_run(writer.run_dir)
    assert result["trace_integrity"] == "complete" and result["run_status"] == status
    assert result["call_count"] == 0 and result["material_count"] is None


@pytest.mark.parametrize("prefix_length,outcome,count", [(3, None, None), (4, None, 1),
                                                       (5, None, 1), (6, "retained", 1)])
def test_hard_kill_prefix_preserves_unknowns(tmp_path, prefix_length, outcome, count):
    writer = fixture_run(tmp_path)
    rewrite(writer, writer.events[:prefix_length])
    result = inspect_run(writer.run_dir)
    assert result["trace_integrity"] == "incomplete" and result["run_status"] == "unknown"
    assert result["generation_outcome"] == outcome
    assert result["call_count"] == count


def test_provider_error_without_terminal_does_not_prove_exhaustion(tmp_path):
    writer = fixture_run(tmp_path)
    events = writer.events[:4]
    events[-1]["payload"].update(status="error", response_complete=False,
                                 error={"type": "operation_error"})
    rewrite(writer, events)
    result = inspect_run(writer.run_dir)
    assert result["provider_errors"] == 1
    assert result["generation_outcome"] is None


@pytest.mark.parametrize("mutation", [
    lambda e: e[1]["payload"].update(chunks=[None]),
    lambda e: e[1]["payload"].update(chunks=["bad"]),
    lambda e: e[1]["payload"].update(chunks=[[]]),
    lambda e: e[1]["payload"].update(selection=None),
    lambda e: e[2]["payload"].update(request=[]),
    lambda e: e[2]["payload"].update(request={}),
    lambda e: e[2]["payload"].update(attempt_index=True),
    lambda e: e[2].update(seq=10),
    lambda e: e[2].update(run_id="other"),
    lambda e: e[2].update(schema_version=9),
    lambda e: e[2].update(time="2026-10-05"),
    lambda e: e[3]["payload"].update(call_id="other"),
    lambda e: e[3]["payload"].update(status=[]),
    lambda e: e[4]["payload"].update(item_count=True),
    lambda e: e[5]["payload"].update(original_index=2),
    lambda e: e[5]["payload"].update(item_id="duplicate"),
    lambda e: e[-1]["payload"].update(status="empty"),
    lambda e: e[-1]["payload"].update(generation_outcome="model_empty"),
    lambda e: e[-1]["payload"].update(artifacts={}),
    lambda e: e[-1]["payload"].update(counts={}),
    lambda e: e[-1]["payload"]["counts"].update(call_count=True),
    lambda e: e[-1]["payload"].update(stage="index"),
    lambda e: e[5]["payload"].update(original_index=True),
])
def test_malformed_relationships_stop_at_valid_prefix(tmp_path, mutation):
    writer = fixture_run(tmp_path)
    mutation(writer.events)
    rewrite(writer, writer.events)
    result = replay_run(writer.run_dir)
    assert result["trace_integrity"] == "incomplete"
    assert result["run_status"] == "unknown"
    assert 1 <= result["last_valid_seq"] < 7


@pytest.mark.parametrize("tail", [b'{"broken"', b'not-json\n', b'\xff\n'])
def test_bad_tail_after_terminal_marks_incomplete(tmp_path, tail):
    writer = fixture_run(tmp_path)
    with (writer.run_dir / "traces.jsonl").open("ab") as stream:
        stream.write(tail)
    result = inspect_run(writer.run_dir)
    assert result["trace_integrity"] == "incomplete"
    assert result["run_status"] == "completed" and result["last_valid_seq"] == 7


def test_damage_in_middle_does_not_skip_forward_to_success(tmp_path):
    writer = fixture_run(tmp_path)
    path = writer.run_dir / "traces.jsonl"
    rows = path.read_bytes().splitlines(keepends=True)
    rows[3] = b'{"broken"\n'
    path.write_bytes(b"".join(rows))
    result = inspect_run(writer.run_dir)
    assert result["last_valid_seq"] == 3 and result["call_count"] is None
    assert result["run_status"] == "unknown"


@pytest.mark.parametrize("version", [2, True, 1.0, "1"])
def test_unknown_manifest_version_is_rejected_without_output(tmp_path, version):
    writer = fixture_run(tmp_path)
    path = writer.run_dir / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["schema_version"] = version
    path.write_text(json.dumps(manifest))
    with pytest.raises(TraceReadError):
        replay_run(writer.run_dir)
    assert not (writer.run_dir / "replays").exists()


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside", "a/b", "a\\b"])
def test_unsafe_artifact_names_are_not_followed(tmp_path, name):
    writer = fixture_run(tmp_path)
    writer.events[-1]["payload"]["artifacts"][name] = "0" * 64
    rewrite(writer, writer.events)
    assert inspect_run(writer.run_dir)["trace_integrity"] == "incomplete"


@pytest.mark.parametrize("name", ["manifest.json", "traces.jsonl", "result.json"])
def test_symlink_inputs_are_rejected_or_incomplete(tmp_path, name):
    writer = fixture_run(tmp_path)
    path = writer.run_dir / name
    other = tmp_path / "outside"
    path.rename(other)
    path.symlink_to(other)
    if name == "manifest.json":
        with pytest.raises(TraceReadError):
            inspect_run(writer.run_dir)
    else:
        assert inspect_run(writer.run_dir)["trace_integrity"] == "incomplete"


def test_symlink_replay_destination_is_rejected(tmp_path):
    writer = fixture_run(tmp_path)
    (writer.run_dir / "replays").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(TraceWriteError):
        replay_run(writer.run_dir)


@pytest.mark.parametrize("mode", ["missing", "modified"])
def test_terminal_is_not_enough_when_artifact_is_missing_or_modified(tmp_path, mode):
    writer = fixture_run(tmp_path)
    path = writer.run_dir / "result.json"
    if mode == "missing":
        path.unlink()
    else:
        path.write_text('{"run_status": "completed"}')
    result = inspect_run(writer.run_dir)
    assert result["run_status"] == "completed" and result["trace_integrity"] == "incomplete"


def test_stored_check_is_not_trusted(tmp_path):
    writer = fixture_run(tmp_path, action="repair")
    writer.events[5]["payload"]["pre_check"] = {"status": "valid", "chunk_ids": ["c1"]}
    rewrite(writer, writer.events)
    result = inspect_run(writer.run_dir)
    assert result["pre_citation_rate"] == 0 and result["post_citation_rate"] == 1
    assert len(result["check_differences"]) == 1


def test_report_neutralizes_untrusted_markdown_and_html():
    report = render_report({"items": [{"question": '# Injected\n<script>alert(1)</script>'}]})
    assert "\n# Injected" not in report and "<script>" not in report
    assert "&lt;script&gt;" in report
    assert "N/A" in report and "not evaluated" in report


def test_report_indents_multiline_scalar_fields():
    report = render_report({"run_id": "fixture\n# injected\n<script>bad</script>"})
    assert "\n# injected" not in report and "<script>" not in report


def test_terminal_fsync_failure_can_leave_self_consistent_files(tmp_path, monkeypatch):
    writer = fixture_run(tmp_path, terminal=False)
    def fail_fsync(_):
        raise OSError("Synthetic confirmation failure")
    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(TraceWriteError):
        writer.finish("completed", "complete", "retained")
    assert writer.failed
    result = inspect_run(writer.run_dir)
    assert result["trace_integrity"] == "complete"
    assert result["run_status"] == "completed"
    assert result["original_cli_exit_status"] is None
