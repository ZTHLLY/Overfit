"""Versioned metadata through local storage, trace, judge and challenge boundaries."""

import copy
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from overfit.evaluation import challenge, judge, runner
from overfit.evaluation.citations import check_citation
from overfit.evaluation.contracts import TraceWriteError
from overfit.evaluation.replay import inspect_run
from overfit.evaluation.report import render_report, summarize
from overfit.evaluation.trace import TraceWriter
from overfit.models import EmbeddedChunk
from overfit.query.selection import spread
from overfit.storage.store import VectorStore
from tests.test_eval_replay import digest, fixture_run
from tests.test_storage import PROFILE, sample_chunk


def trace_with_chunk(tmp_path, chunk):
    writer = TraceWriter.create(tmp_path, {"course": "isolated", "questions": 1}, {})
    materials = [asdict(chunk)]
    item = {"question": "Is the converse implied?", "answer": "No.", "topic": "Logic",
            "source": chunk.source, "page": chunk.page}
    writer.emit("materials_selected", {"chunks": materials, "materials_sha256": digest(materials),
                "selection": {"actual_count": 1}, "index": {}})
    call = writer.start_call(1, 1, {"model": "fake", "stream": True, "temperature": 0,
                "timeout": 30, "response_format": None,
                "messages": [{"role": "user", "content": chunk.text}]})
    writer.emit("call_finished", {"call_id": call, "status": "completed", "content": json.dumps({"items": [item]}),
                "response_complete": True, "elapsed_seconds": 0, "error": None, "usage": None})
    writer.emit("validation_finished", {"call_id": call, "status": "valid", "item_count": 1, "error": None})
    writer.emit("citation_processed", {"call_id": call, "item_id": f"{call}-item-1", "original_index": 1,
                "final_index": 1, "pre_policy": item, "post_policy": item, "action": "keep", "reason": "fixture",
                "pre_check": check_citation(item, materials), "post_check": check_citation(item, materials)})
    result = summarize(writer.manifest, writer.events)
    writer.write_artifact("result.json", json.dumps(result))
    writer.write_artifact("report.md", render_report(result))
    writer.write_artifact("isolated_mock_exam.md", "exam")
    writer.write_artifact("isolated_answers.md", "answers")
    writer.finish("completed", "complete", "retained")
    return writer, item


def suite_with_chunk(tmp_path, chunk, item):
    original = Path(__file__).parents[1] / "eval/judge_challenges/ifn580_v1"
    inputs = json.loads((original / "inputs.json").read_text())
    annotations = json.loads((original / "annotations.json").read_text())
    inputs.update(schema="overfit.judge_challenge_inputs.v2", chunks=[asdict(chunk)],
                  cases=[{"case_id": "case-001", "input": item}])
    annotations.update(schema="overfit.judge_challenge_annotations.v2", cases=annotations["cases"][:1])
    directory = tmp_path / "v2-suite"
    directory.mkdir()
    for name, value in [("inputs", inputs), ("annotations", annotations)]:
        (directory / f"{name}.json").write_text(json.dumps(value))
    return directory


def test_store_selection_trace_judge_and_challenge_preserve_metadata_without_extra_text(tmp_path):
    chunk = sample_chunk()
    audit = copy.deepcopy(chunk.provenance["blocks"][0])
    audit.update(block_id="audit", text="AUDIT_ONLY_SENTINEL", text_span=None,
                 block_span=None, mapping="coarse")
    chunk.provenance["blocks"].append(audit)
    with VectorStore.open(tmp_path / "isolated.db", PROFILE, create=True) as store:
        store.add([EmbeddedChunk(chunk, [1.0, 0.0])])
        selected = spread(store.all_embedded(), 1)[0].chunk
    assert selected.provenance == chunk.provenance
    writer, item = trace_with_chunk(tmp_path, selected)
    assert writer.manifest["schema_version"] == 2
    event = next(e for e in writer.events if e["event_type"] == "materials_selected")
    assert event["payload"]["materials_schema_version"] == 2
    assert event["payload"]["chunks"][0]["provenance"] == chunk.provenance
    assert inspect_run(writer.run_dir)["trace_integrity"] == "complete"
    prepared = runner.run_judge(writer.run_dir)
    assert prepared["status"] == "prepared"
    tasks = json.loads((Path(prepared["judgment_dir"]) / "tasks.json").read_text())
    assert tasks["schema_version"] == 4 and tasks["chunks"][0]["provenance"] == chunk.provenance
    settings = judge.JudgeSettings(base_url="https://fake.example", model="fake", api_key="secret", _env_file=None)
    for phase in ("answerability", "support"):
        request = judge.build_request(phase, item, tasks["chunks"], settings)
        content = json.dumps(request)
        assert "AUDIT_ONLY_SENTINEL" not in content
        data = json.loads(request["messages"][1]["content"])
        assert data["materials"][0]["text"] == chunk.text
        if phase == "answerability":
            assert "answer" not in data and "citation" not in data
    suite = suite_with_chunk(tmp_path, selected, item)
    original = {p.name: p.read_bytes() for p in suite.iterdir()}
    result = challenge.run_challenge(suite, output_dir=tmp_path / "challenges")
    destination = Path(result["challenge_dir"])
    for name, raw in original.items():
        assert (destination / name).read_bytes() == raw
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["schema_version"] == 2 and manifest["judge_protocol_version"] == "judge-v4"
    assert manifest["anchor_origin"] == "challenge_input"
    assert manifest["input_metadata"] == "explicit"


def test_new_trace_rejects_invalid_metadata_before_material_event(tmp_path):
    writer = TraceWriter.create(tmp_path, {"course": "test", "questions": 1}, {})
    material = asdict(sample_chunk())
    material["provenance"]["page_count"] = True
    with pytest.raises(TraceWriteError):
        writer.emit("materials_selected", {"chunks": [material], "materials_sha256": digest([material]),
                    "selection": {"actual_count": 1}, "index": {}})
    assert [e["event_type"] for e in writer.events] == ["run_started"]


def test_legacy_trace_one_is_read_without_metadata_or_rewriting(tmp_path):
    writer = fixture_run(tmp_path)
    manifest = copy.deepcopy(writer.manifest)
    manifest["schema_version"] = 1
    events = copy.deepcopy(writer.events)
    for event in events:
        event["schema_version"] = 1
        event["payload"].pop("materials_schema_version", None)
    (writer.run_dir / "manifest.json").write_text(json.dumps(manifest))
    # The old strict result stays a schema-1 artifact, not a new-policy reinterpretation.
    result = summarize(manifest, events[:-1])
    (writer.run_dir / "result.json").write_text(json.dumps(result))
    (writer.run_dir / "report.md").write_text(render_report(result))
    for name in events[-1]["payload"]["artifacts"]:
        events[-1]["payload"]["artifacts"][name] = runner.digest((writer.run_dir / name).read_bytes())
    (writer.run_dir / "traces.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    before = {p.name: p.read_bytes() for p in writer.run_dir.iterdir()}
    result = inspect_run(writer.run_dir)
    assert result["trace_integrity"] == "complete" and result["schema_version"] == 1
    _, _, tasks = runner._source(writer.run_dir)
    assert "provenance" not in tasks["chunks"][0]
    assert before == {p.name: p.read_bytes() for p in writer.run_dir.iterdir()}


def test_legacy_trace_cannot_smuggle_metadata_as_old_schema(tmp_path):
    writer = fixture_run(tmp_path)
    manifest = writer.manifest | {"schema_version": 1}
    (writer.run_dir / "manifest.json").write_text(json.dumps(manifest))
    events = copy.deepcopy(writer.events)
    for event in events:
        event["schema_version"] = 1
        event["payload"].pop("materials_schema_version", None)
        if event["event_type"] == "materials_selected":
            event["payload"]["chunks"][0]["provenance"] = None
            event["payload"]["materials_sha256"] = digest(event["payload"]["chunks"])
    (writer.run_dir / "traces.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    assert inspect_run(writer.run_dir)["trace_integrity"] == "incomplete"


def test_judge_v3_record_rejected_before_reinterpretation(tmp_path):
    directory = tmp_path / "old"
    directory.mkdir()
    (directory / "complete.json").write_text('{"schema_version":3}')
    with pytest.raises(runner.JudgeRunError, match="Unsupported judge recording version"):
        runner._reconstruct(directory)


def test_challenge_v1_snapshot_stays_unmodified_and_marks_legacy_unknown(tmp_path):
    suite = Path(__file__).parents[1] / "eval/judge_challenges/ifn580_v1"
    before = {name: (suite / name).read_bytes() for name in ("inputs.json", "annotations.json")}
    result = challenge.run_challenge(suite, output_dir=tmp_path / "outputs")
    directory = Path(result["challenge_dir"])
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["input_metadata"] == "legacy_unknown" and manifest["protocol_version"] == "challenge-v2"
    assert before == {name: (suite / name).read_bytes() for name in before}
    assert before == {name: (directory / name).read_bytes() for name in before}


@pytest.mark.parametrize("anchor,expected", [(7, "verified"), (9, "source_issue"), (13, "source_issue")])
def test_v2_challenge_allows_adjacent_or_outside_anchor_for_separate_diagnostics(tmp_path, anchor, expected):
    from overfit.evaluation.elastic import locate_citation
    chunk = sample_chunk()
    # Isolate a mapped physical page: parent sample is deliberately cross-page.
    chunk.provenance["blocks"][0]["locations"] = [{"page": 6, "charspan": None, "bbox": None}]
    item = {"source": chunk.source, "page": anchor, "question": "Q", "answer": "A", "topic": "T"}
    suite = suite_with_chunk(tmp_path, chunk, item)
    _, inputs, _, _ = challenge.load_suite(suite)
    assert check_citation(item, inputs["chunks"])["status"] == "page_not_supplied"
    result = locate_citation(item, inputs["chunks"], inputs["chunks"][0], anchor_origin="challenge_input")
    assert result["status"] == expected
    if expected == "source_issue":
        content = json.dumps({"material_support": "supported", "citation_support": "supported",
                              "reason": "fixed model verdict", "claims": [{"text": "claim", "verdict": "supported",
                              "evidence": [{"chunk_id": chunk.id}]}], "citation_evidence": [{"chunk_id": chunk.id}]})
        state = runner._classify("support", {"status": "response", "response": {"content": content, "finish_reason": "stop"}},
                                 item, inputs["chunks"], anchor_origin="challenge_input")
        assert state["human_review"]["status"] == "pending"
        assert state["model_review"]["citation_support"] == "supported"


def test_v1_challenge_preserves_strict_entry_rejection(tmp_path):
    chunk = sample_chunk()
    item = {"source": chunk.source, "page": 7, "question": "Q", "answer": "A", "topic": "T"}
    suite = suite_with_chunk(tmp_path, chunk, item)
    for name in ("inputs", "annotations"):
        path = suite / f"{name}.json"
        value = json.loads(path.read_text())
        value["schema"] = value["schema"].replace(".v2", ".v1")
        if name == "inputs":
            value["chunks"][0].pop("provenance")
        path.write_text(json.dumps(value))
    with pytest.raises(challenge.ChallengeError):
        challenge.load_suite(suite)


@pytest.mark.parametrize("value", [1, True, 2.0, "2", 3])
def test_writer_does_not_coerce_material_schema(tmp_path, value):
    writer = TraceWriter.create(tmp_path, {"course": "test", "questions": 1}, {})
    materials = [asdict(sample_chunk())]
    with pytest.raises(TraceWriteError):
        writer.emit("materials_selected", {"materials_schema_version": value, "chunks": materials,
                    "materials_sha256": digest(materials), "selection": {"actual_count": 1}, "index": {}})


def test_missing_manifest_version_is_not_upgraded_to_writer_default(tmp_path):
    from overfit.evaluation.contracts import TraceReadError
    writer = fixture_run(tmp_path)
    manifest = copy.deepcopy(writer.manifest)
    manifest.pop("schema_version")
    (writer.run_dir / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(TraceReadError, match="unsupported manifest"):
        inspect_run(writer.run_dir)


def test_missing_event_version_rejects_suffix_not_upgrade_to_writer_default(tmp_path):
    writer = fixture_run(tmp_path)
    events = copy.deepcopy(writer.events)
    events[1].pop("schema_version")
    (writer.run_dir / "traces.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    result = inspect_run(writer.run_dir)
    assert result["trace_integrity"] == "incomplete"
    assert result["last_valid_seq"] == 1
    assert result["material_count"] is None


def test_duplicate_declared_version_is_not_accepted(tmp_path):
    from overfit.evaluation.contracts import TraceReadError
    writer = fixture_run(tmp_path)
    path = writer.run_dir / "manifest.json"
    path.write_text(path.read_text().replace('"schema_version": 2', '"schema_version": 1, "schema_version": 2'))
    with pytest.raises(TraceReadError):
        inspect_run(writer.run_dir)
