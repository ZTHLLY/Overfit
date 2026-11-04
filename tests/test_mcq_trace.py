"""mcq-v1 traces replay beside, rather than rewriting, legacy recordings."""

import hashlib
import json
from copy import deepcopy

import pytest

from overfit.evaluation.citations import check_citation
from overfit.evaluation.replay import _validate_mcq_event, inspect_run, replay_run
from overfit.evaluation.report import render_report, summarize
from overfit.evaluation.trace import TraceWriter
from overfit.models import GeneratedMCQExam, GeneratedMCQItem
from overfit.query.generator import render_exam
from overfit.query.mcq import balance_options, mix_items


def _digest(value) -> str:
    data = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def _item():
    return {
        "topic": "Overfitting",
        "question": "Which statement best describes overfitting?",
        "options": {
            label: {
                "text": f"Option {label}",
                "explanation": f"Feedback {label}.",
                "source": "notes.pdf",
                "page": 5,
                "page_end": None,
            }
            for label in "ABCD"
        },
        "correct_option": "B",
    }


def test_mcq_trace_replays_with_contract_and_batch_identity(tmp_path):
    plan = [{"index": 1, "question_count": 1}]
    writer = TraceWriter.create(
        tmp_path,
        {
            "course": "COURSE",
            "questions": 1,
            "material_budget": 1,
            "generation_contract": "mcq-v1",
            "batch_plan": plan,
            "seed_identity": "seed-1",
        },
        {},
    )
    chunks = [{
        "id": "c1", "text": "Evidence", "source": "notes.pdf", "page": 5,
        "page_end": None, "section": None, "provenance": None,
    }]
    item = _item()
    writer.emit("batch_checkpoint", {
        "batch_index": 0, "question_count": 1, "state": "planned",
    })
    writer.emit("batch_checkpoint", {
        "batch_index": 1, "question_count": 1, "state": "running",
    })
    writer.emit("materials_selected", {
        "batch_index": 1,
        "chunks": chunks,
        "selection": {"actual_count": 1},
        "index": {},
        "materials_sha256": _digest(chunks),
    })
    call_id = "call-1"
    writer.emit("call_started", {
        "batch_index": 1,
        "call_id": call_id,
        "attempt_index": 1,
        "format_index": 1,
        "request": {
            "model": "fixture", "stream": True, "temperature": 0.2,
            "timeout": 30, "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": "prompt"}],
        },
    })
    writer.emit("call_finished", {
        "call_id": call_id, "status": "completed",
        "content": json.dumps({"questions": [item]}),
        "response_complete": True, "elapsed_seconds": 0.1,
        "error": None, "usage": None,
    })
    writer.emit("validation_finished", {
        "call_id": call_id, "status": "valid", "item_count": 1, "error": None,
    })
    checks = {label: check_citation(option, chunks) for label, option in item["options"].items()}
    writer.emit("citation_processed", {
        "batch_index": 1, "call_id": call_id, "item_id": "call-1-item-1",
        "original_index": 1, "final_index": 1, "pre_policy": item,
        "post_policy": item, "action": "keep", "reason": "citation_supplied",
        "option_checks": checks,
    })
    writer.emit("batch_checkpoint", {
        "batch_index": 1, "question_count": 1, "state": "completed",
    })
    writer.emit("deduplication_finished", {"items": [item], "removed": []})
    balanced = balance_options(
        [GeneratedMCQItem.model_validate(item)], seed_identity="seed-1"
    )
    balanced_items = [value.model_dump() for value in balanced.items]
    final_items = [
        value.model_dump() for value in mix_items(
            balanced.items, seed_identity="seed-1"
        )
    ]
    writer.emit("options_balanced", {
        "items": final_items, "mappings": balanced.mappings,
        "balanced_items": balanced_items, "presentation_order": [0],
        "item_material_batches": [1],
        "seed_identity": "seed-1",
    })
    summary = summarize(writer.manifest, writer.events)
    writer.write_artifact(
        "result.json",
        json.dumps(summary, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
    )
    writer.write_artifact("report.md", render_report(summary))
    paper, answers = render_exam(
        "COURSE",
        GeneratedMCQExam.model_validate({"items": final_items}),
        answers_file="COURSE_answers.md",
        generated_on=writer.manifest["started_at"][:10],
    )
    writer.write_artifact("COURSE_mock_exam.md", paper)
    writer.write_artifact("COURSE_answers.md", answers)
    writer.finish("completed", "complete", "retained")

    result = replay_run(writer.run_dir)

    assert result["trace_integrity"] == "complete"
    assert result["generation_contract"] == "mcq-v1"
    assert result["retained_count"] == 1
    assert result["items"] == final_items

    log = writer.run_dir / "traces.jsonl"
    original = log.read_text()
    rows = [json.loads(line) for line in original.splitlines()]
    finished_row = next(row for row in rows if row["event_type"] == "call_finished")
    finished_row["payload"]["content"] = "{}"
    log.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert inspect_run(writer.run_dir)["trace_integrity"] == "incomplete"

    log.write_text(original)
    rows = [json.loads(line) for line in original.splitlines()]
    balance_row = next(row for row in rows if row["event_type"] == "options_balanced")
    balance_row["payload"]["mappings"][0] = {label: "A" for label in "ABCD"}
    log.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert inspect_run(writer.run_dir)["trace_integrity"] == "incomplete"

    log.write_text(original)
    rows = [json.loads(line) for line in original.splitlines()]
    citation_row = next(row for row in rows if row["event_type"] == "citation_processed")
    citation_row["payload"].update(action="drop", post_policy=None, final_index=None)
    log.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert inspect_run(writer.run_dir)["trace_integrity"] == "incomplete"

    log.write_text(original)
    rows = [json.loads(line) for line in original.splitlines()]
    citation_row = next(row for row in rows if row["event_type"] == "citation_processed")
    citation_row["payload"]["batch_index"] = 99
    log.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert inspect_run(writer.run_dir)["trace_integrity"] == "incomplete"

    log.write_text(original)
    result_path = writer.run_dir / "result.json"
    result_path.write_text("{}\n")
    rows = [json.loads(line) for line in original.splitlines()]
    terminal = next(row for row in rows if row["event_type"] == "run_finished")
    terminal["payload"]["artifacts"]["result.json"] = hashlib.sha256(
        result_path.read_bytes()
    ).hexdigest()
    log.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert inspect_run(writer.run_dir)["trace_integrity"] == "incomplete"

    result_path.write_text(
        json.dumps(summary, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    )
    log.write_text(original)
    rows = [json.loads(line) for line in original.splitlines()]
    terminal = next(row for row in rows if row["event_type"] == "run_finished")
    terminal["payload"].update(
        status="failed", stage="index", generation_outcome="failed", error={}
    )
    log.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert inspect_run(writer.run_dir)["trace_integrity"] == "incomplete"


def test_mcq_replay_requires_writer_format_fallback_before_next_attempt(tmp_path):
    writer = TraceWriter.create(
        tmp_path,
        {
            "course": "COURSE", "questions": 1, "material_budget": 1,
            "generation_contract": "mcq-v1",
            "batch_plan": [{"index": 1, "question_count": 1}],
            "seed_identity": "seed-1",
        },
        {"max_attempts": 3},
    )
    writer.emit("batch_checkpoint", {
        "batch_index": 0, "question_count": 1, "state": "planned",
    })
    writer.emit("batch_checkpoint", {
        "batch_index": 1, "question_count": 1, "state": "running",
    })
    chunks = [{
        "id": "c1", "text": "Evidence", "source": "notes.pdf", "page": 5,
        "page_end": None, "section": None, "provenance": None,
    }]
    writer.emit("materials_selected", {
        "batch_index": 1, "chunks": chunks, "selection": {"actual_count": 1},
        "index": {}, "materials_sha256": _digest(chunks),
    })
    request = {
        "model": "fixture", "stream": True, "temperature": 0.2, "timeout": 30,
        "messages": [{"role": "user", "content": "prompt"}],
    }
    writer.emit("call_started", {
        "batch_index": 1, "call_id": "failed", "attempt_index": 1,
        "format_index": 1, "request": {**request, "response_format": {"type": "json_object"}},
    })
    writer.emit("call_finished", {
        "call_id": "failed", "status": "error", "content": "",
        "response_complete": False, "elapsed_seconds": 0.1,
        "error": {"type": "operation_error", "message": "Operation failed."},
        "usage": None,
    })
    invalid = deepcopy(writer.events[-2])
    invalid["seq"] = len(writer.events) + 1
    invalid["payload"].update(call_id="skipped-fallback", attempt_index=2, format_index=1)
    with pytest.raises(ValueError):
        _validate_mcq_event(invalid, writer.events, writer.manifest)
