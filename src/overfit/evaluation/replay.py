"""Read-only reconstruction of a recording, with new, separate replay artifacts.

No runtime settings, model client, generator, or database imports belong here.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from overfit.evaluation.contracts import (
    Event,
    Manifest,
    TraceReadError,
    TraceWriteError,
)
from overfit.evaluation.report import render_report, summarize


def _require(condition, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_digest(value) -> str:
    return _digest(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode())


def _file(root: Path, name: str) -> Path:
    """Artifacts are flat filenames, not paths supplied by a manifest."""
    if (not isinstance(name, str) or not name or name in {".", ".."}
            or "/" in name or "\\" in name or Path(name).is_absolute()):
        raise TraceReadError("Invalid artifact path.")
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise TraceReadError("Artifact symlinks are not supported.")
    return path


def _read(root: Path, name: str) -> bytes:
    try:
        return _file(root, name).read_bytes()
    except OSError:
        raise TraceReadError("Required recording file is not readable.") from None


def _validate_event(event: dict, accepted: list[dict], manifest: dict) -> None:
    """Validate stage-dependent relationships, not just an envelope shape."""
    if manifest.get("task", {}).get("generation_contract") == "mcq-v1":
        _validate_mcq_event(event, accepted, manifest)
        return
    kind, p = event["event_type"], event["payload"]
    _require(event["run_id"] == manifest["run_id"], "Event run ID mismatch.")
    _require(event["schema_version"] == manifest["schema_version"], "Trace envelope version mismatch.")
    _require(event["seq"] == len(accepted) + 1, "Event sequence mismatch.")
    stamp = datetime.fromisoformat(event["time"])
    _require(stamp.utcoffset() is not None and stamp.utcoffset().total_seconds() == 0,
             "Event timestamp must be UTC.")
    def prior(kind):
        return [e["payload"] for e in accepted if e["event_type"] == kind]
    _require(not prior("run_finished"), "Events after terminal marker.")
    if not accepted:
        _require(kind == "run_started", "Missing run start.")
    if kind == "run_started":
        _require(not accepted and p.get("task") == manifest["task"], "Invalid run start.")
        return
    _require(bool(accepted), "Missing run start.")
    materials = prior("materials_selected")
    starts, finishes = prior("call_started"), prior("call_finished")
    validations = prior("validation_finished")
    valid = [v for v in validations if v["status"] == "valid"]
    citations = prior("citation_processed")
    pending = [s for s in starts if not any(f["call_id"] == s["call_id"] for f in finishes)]
    if kind == "materials_selected":
        _require(not materials and not starts, "Invalid material event order.")
        from overfit.evaluation.elastic import validate_materials
        if manifest["schema_version"] == 2:
            _require(type(p.get("materials_schema_version")) is int
                     and p["materials_schema_version"] == 2, "Invalid material schema.")
        else:
            _require("materials_schema_version" not in p, "Legacy materials must not claim a new schema.")
        chunks = p["chunks"]
        _require(isinstance(chunks, list), "Invalid materials.")
        validate_materials(chunks, legacy=manifest["schema_version"] == 1)
        _require(all(isinstance(c.get("text"), str) for c in chunks), "Missing material text.")
        _require(len({c["id"] for c in chunks}) == len(chunks), "Duplicate chunk IDs.")
        _require(p["materials_sha256"] == _json_digest(chunks), "Material digest mismatch.")
        _require(_integer(p["selection"]["actual_count"])
                 and p["selection"]["actual_count"] == len(chunks), "Material count mismatch.")
        _require(isinstance(p["index"], dict), "Missing index identity.")
    elif kind == "call_started":
        _require(bool(materials) and not pending and not valid, "Invalid call start order.")
        _require(isinstance(p["call_id"], str) and p["call_id"], "Invalid call ID.")
        _require(not any(s["call_id"] == p["call_id"] for s in starts), "Duplicate call ID.")
        _require(_integer(p["attempt_index"], 1) and _integer(p["format_index"], 1),
                 "Invalid attempt index.")
        if starts:
            last, finished = starts[-1], finishes[-1]
            previous_validation = [v for v in validations if v["call_id"] == last["call_id"]]
            _require(finished["status"] == "error" or bool(previous_validation),
                     "Missing validation before next call.")
            same_attempt = p["attempt_index"] == last["attempt_index"]
            if same_attempt:
                _require(finished["status"] == "error"
                         and p["format_index"] == last["format_index"] + 1,
                         "Invalid format fallback order.")
            else:
                _require(p["attempt_index"] == last["attempt_index"] + 1
                         and p["format_index"] == 1, "Invalid retry order.")
        else:
            _require(p["attempt_index"] == p["format_index"] == 1, "Invalid first attempt.")
        request = p["request"]
        _require(isinstance(request, dict) and isinstance(request["model"], str)
                 and request["stream"] is True and _number(request["temperature"])
                 and _number(request["timeout"]) and request["timeout"] > 0,
                 "Invalid request snapshot.")
        _require(request["response_format"] is None
                 or isinstance(request["response_format"], dict), "Invalid response format.")
        _require(isinstance(request["messages"], list) and request["messages"]
                 and all(isinstance(m, dict) and m.get("role") in {"system", "user", "assistant"}
                         and isinstance(m.get("content"), str) for m in request["messages"]),
                 "Invalid message snapshot.")
    elif kind == "call_finished":
        _require(len(pending) == 1 and pending[0]["call_id"] == p["call_id"],
                 "Unpaired call finish.")
        _require(p["status"] in {"completed", "error"} and isinstance(p["content"], str),
                 "Invalid call result.")
        _require(p["response_complete"] is (p["status"] == "completed"),
                 "Invalid response completeness.")
        _require(_number(p["elapsed_seconds"]) and p["elapsed_seconds"] >= 0,
                 "Invalid call timing.")
        _require(p["usage"] is None or isinstance(p["usage"], dict), "Invalid usage.")
        _require((p["error"] is None) if p["status"] == "completed"
                 else isinstance(p["error"], dict), "Invalid call error.")
    elif kind == "validation_finished":
        _require(bool(finishes) and not pending and not valid
                 and finishes[-1]["call_id"] == p["call_id"]
                 and finishes[-1]["status"] == "completed", "Invalid validation order.")
        _require(not any(v["call_id"] == p["call_id"] for v in validations),
                 "Duplicate validation.")
        _require(p["status"] in {"valid", "invalid", "empty_body"}, "Invalid validation status.")
        _require(_integer(p["item_count"]) if p["status"] == "valid"
                 else p["item_count"] is None, "Invalid validated item count.")
        if p["status"] == "empty_body":
            _require(not finishes[-1]["content"].strip(), "Nonempty response marked empty.")
    elif kind == "citation_processed":
        _require(bool(valid) and not pending, "Citation before schema validation.")
        _require(p["call_id"] == valid[-1]["call_id"], "Citation call mismatch.")
        ordinal = len(citations) + 1
        _require(_integer(p["original_index"], 1)
                 and p["original_index"] == ordinal and ordinal <= valid[-1]["item_count"],
                 "Invalid item ordinal.")
        _require(p["item_id"] == f'{p["call_id"]}-item-{ordinal}', "Invalid item identity.")
        _require(p["action"] in {"keep", "repair", "drop"}, "Invalid citation action.")
        before, after = p["pre_policy"], p["post_policy"]
        _require(isinstance(before, dict) and all(isinstance(before.get(k), str)
                 for k in ("topic", "question", "answer", "source"))
                 and type(before.get("page")) is int, "Invalid normalized item.")
        if p["action"] == "drop":
            _require(after is None and p["final_index"] is None, "Invalid drop mapping.")
        else:
            final = sum(i["post_policy"] is not None for i in citations) + 1
            _require(isinstance(after, dict) and _integer(p["final_index"], 1)
                     and p["final_index"] == final,
                     "Invalid final item mapping.")
            _require({k: v for k, v in before.items() if k != "page"}
                     == {k: v for k, v in after.items() if k != "page"},
                     "Citation policy changed item content.")
            _require(type(after.get("page")) is int, "Invalid final page.")
            if p["action"] == "keep":
                _require(before == after, "Keep action changed item.")
            else:
                _require(before["page"] != after["page"] and
                         abs(before["page"] - after["page"]) <= 2, "Invalid page repair.")
    elif kind == "run_finished":
        _require(not pending, "Unfinished call at terminal event.")
        _require(p["status"] in {"completed", "empty", "failed", "interrupted"},
                 "Invalid run status.")
        _require(p["stage"] in {"initialization", "index", "selection", "generation",
                                 "artifacts", "complete"}, "Invalid failure stage.")
        expected_counts = {"call_started": len(starts), "call_finished": len(finishes),
                           "unresolved": 0, "call_count": len(finishes)}
        _require(isinstance(p["counts"], dict) and p["counts"] == expected_counts
                 and all(_integer(value) for value in p["counts"].values()),
                 "Terminal counts disagree with events.")
        computed = summarize(manifest, accepted)
        declared_failure = (p["generation_outcome"] == "failed"
                            and p["status"] == "failed" and p["stage"] == "generation"
                            and bool(starts) and not valid)
        _require(declared_failure
                 or p["generation_outcome"] == computed["generation_outcome"],
                 "Generation outcome does not match observed events.")
        if p["status"] in {"completed", "empty"}:
            _require(p["stage"] == "complete" and p["error"] is None,
                     "Invalid success terminal metadata.")
            _require(bool(valid) and len(citations) == valid[-1]["item_count"],
                     "Incomplete item processing.")
            expected = "completed" if computed["retained_count"] else "empty"
            _require(p["status"] == expected, "Run status conflicts with item count.")
        elif not materials:
            _require(not starts and p["stage"] in {"initialization", "index", "selection"},
                     "Late failure has no materials.")
        artifacts = p["artifacts"]
        _require(isinstance(artifacts, dict) and "manifest.json" in artifacts,
                 "Missing manifest integrity record.")
        _require("traces.jsonl" not in artifacts, "Self-referencing log hash.")
        if p["status"] in {"completed", "empty"}:
            _require({"result.json", "report.md"} <= artifacts.keys(), "Missing result artifacts.")
        if p["status"] == "completed":
            _require(any(n.endswith("_mock_exam.md") for n in artifacts)
                     and any(n.endswith("_answers.md") for n in artifacts), "Missing exam artifacts.")
        _require(all(isinstance(d, str) and re.fullmatch(r"[0-9a-f]{64}", d)
                     for d in artifacts.values()), "Invalid artifact digest.")


def _valid_mcq_item(value) -> bool:
    from overfit.models import GeneratedMCQItem

    try:
        GeneratedMCQItem.model_validate(value)
    except (ValidationError, TypeError, ValueError):
        return False
    return True


def _reconstruct_mcq_content(content: str, max_items: int):
    from overfit.models import GeneratedMCQExam
    from overfit.query.mcq_wire import extract_json, normalize_mcq_wire

    value = GeneratedMCQExam.model_validate(
        normalize_mcq_wire(extract_json(content))
    )
    if len(value.items) > max_items:
        raise ValueError("MCQ response exceeded requested batch count")
    return value


def _validate_mcq_event(event: dict, accepted: list[dict], manifest: dict) -> None:
    """Validate mcq-v1 ordering while leaving semantic judgments to Judge."""
    kind, payload = event["event_type"], event["payload"]
    _require(event["run_id"] == manifest["run_id"], "Event run ID mismatch.")
    _require(event["schema_version"] == manifest["schema_version"], "Trace envelope version mismatch.")
    _require(event["seq"] == len(accepted) + 1, "Event sequence mismatch.")
    stamp = datetime.fromisoformat(event["time"])
    _require(stamp.utcoffset() is not None and stamp.utcoffset().total_seconds() == 0,
             "Event timestamp must be UTC.")

    def rows(name):
        return [row["payload"] for row in accepted if row["event_type"] == name]

    _require(not rows("run_finished"), "Events after terminal marker.")
    if kind == "run_started":
        _require(not accepted and payload.get("task") == manifest["task"], "Invalid run start.")
        return
    _require(bool(accepted), "Missing run start.")
    plan = manifest["task"]["batch_plan"]
    planned = {row["index"]: row["question_count"] for row in plan}
    starts, finishes = rows("call_started"), rows("call_finished")
    validations = rows("validation_finished")
    pending = [row for row in starts if not any(
        finish["call_id"] == row["call_id"] for finish in finishes
    )]

    if kind == "batch_checkpoint":
        batch = payload.get("batch_index")
        state = payload.get("state")
        _require(state in {"planned", "running", "completed", "failed", "interrupted"},
                 "Invalid batch state.")
        if state == "planned":
            _require(batch == 0 and not rows("batch_checkpoint"), "Invalid batch plan marker.")
            _require(payload.get("question_count") == manifest["task"]["questions"],
                     "Invalid planned count.")
        else:
            _require(batch in planned and payload.get("question_count") == planned[batch],
                     "Invalid batch identity.")
            prior = rows("batch_checkpoint")
            _require(prior and prior[0]["state"] == "planned", "Missing batch plan marker.")
            states = [row["state"] for row in prior if row["batch_index"] == batch]
            if state == "running":
                _require(not states and all(
                    any(row["batch_index"] == previous and row["state"] == "completed" for row in prior)
                    for previous in range(1, batch)
                ), "Invalid batch start order.")
            else:
                _require(states == ["running"] and not pending, "Invalid terminal batch state.")
                if state == "completed":
                    valid_calls = {
                        row["call_id"] for row in validations if row["status"] == "valid"
                    }
                    batch_calls = [row for row in starts if row.get("batch_index") == batch]
                    _require(batch_calls and batch_calls[-1]["call_id"] in valid_calls,
                             "Completed batch has no valid result.")
                    final_call = batch_calls[-1]["call_id"]
                    final_validation = next(
                        row for row in validations if row["call_id"] == final_call
                    )
                    processed = [
                        row for row in rows("citation_processed")
                        if row["call_id"] == final_call
                    ]
                    _require(len(processed) == final_validation["item_count"],
                             "Completed batch has incomplete item processing.")
        return

    if kind == "materials_selected":
        batch = payload.get("batch_index")
        _require(batch in planned and not any(row.get("batch_index") == batch for row in rows(kind)),
                 "Invalid material batch identity.")
        _require(any(row["batch_index"] == batch and row["state"] == "running"
                     for row in rows("batch_checkpoint")), "Materials before batch start.")
        chunks = payload.get("chunks")
        from overfit.evaluation.elastic import validate_materials
        validate_materials(chunks)
        _require(payload.get("materials_schema_version") == 2, "Invalid material schema.")
        _require(payload.get("materials_sha256") == _json_digest(chunks), "Material digest mismatch.")
        _require(payload.get("selection", {}).get("actual_count") == len(chunks),
                 "Material count mismatch.")
        _require(isinstance(payload.get("index"), dict), "Missing index identity.")
        return

    if kind == "call_started":
        batch = payload.get("batch_index")
        _require(batch in planned and not pending, "Invalid call batch or overlap.")
        _require(any(row.get("batch_index") == batch for row in rows("materials_selected")),
                 "Call before batch materials.")
        batch_starts = [row for row in starts if row.get("batch_index") == batch]
        _require(len(batch_starts) < 3, "Batch provider-call ceiling exceeded.")
        _require(isinstance(payload.get("call_id"), str) and payload["call_id"], "Invalid call ID.")
        _require(not any(row["call_id"] == payload["call_id"] for row in starts), "Duplicate call ID.")
        _require(
            _integer(payload.get("attempt_index"), 1)
            and payload.get("format_index") in {1, 2},
            "Invalid call index.",
        )
        if batch_starts:
            previous = batch_starts[-1]
            previous_finish = next(
                row for row in finishes if row["call_id"] == previous["call_id"]
            )
            previous_validation = next(
                (row for row in validations if row["call_id"] == previous["call_id"]),
                None,
            )
            if previous_finish["status"] == "error" and previous["format_index"] == 1:
                _require(
                    payload["attempt_index"] == previous["attempt_index"]
                    and payload["format_index"] == 2,
                    "Invalid MCQ format fallback order.",
                )
            else:
                _require(
                    payload["attempt_index"] == previous["attempt_index"] + 1
                    and payload["format_index"] == 1
                    and (
                        (previous_finish["status"] == "error"
                         and previous["format_index"] == 2)
                        or (
                            previous_finish["status"] == "completed"
                            and previous_validation is not None
                            and previous_validation["status"] in {"invalid", "empty_body"}
                        )
                    ),
                    "Invalid MCQ validation retry order.",
                )
        else:
            _require(payload["attempt_index"] == payload["format_index"] == 1,
                     "Invalid first MCQ attempt.")
        request = payload.get("request")
        _require(isinstance(request, dict) and isinstance(request.get("model"), str)
                 and request.get("stream") is True and _number(request.get("temperature"))
                 and _number(request.get("timeout")) and request["timeout"] > 0,
                 "Invalid request snapshot.")
        _require(isinstance(request.get("messages"), list) and request["messages"],
                 "Invalid request messages.")
        _require(
            request.get("response_format")
            == ({"type": "json_object"} if payload["format_index"] == 1 else None),
            "Request format does not match fallback index.",
        )
        return

    if kind == "call_finished":
        _require(len(pending) == 1 and pending[0]["call_id"] == payload.get("call_id"),
                 "Unpaired call finish.")
        _require(payload.get("status") in {"completed", "error"}
                 and isinstance(payload.get("content"), str), "Invalid call result.")
        _require(payload.get("response_complete") is (payload["status"] == "completed"),
                 "Invalid response completeness.")
        _require(_number(payload.get("elapsed_seconds")) and payload["elapsed_seconds"] >= 0,
                 "Invalid call timing.")
        return

    if kind == "validation_finished":
        _require(finishes and not pending and finishes[-1]["call_id"] == payload.get("call_id")
                 and finishes[-1]["status"] == "completed", "Invalid validation order.")
        _require(not any(row["call_id"] == payload["call_id"] for row in validations),
                 "Duplicate validation.")
        _require(payload.get("status") in {"valid", "invalid", "empty_body"},
                 "Invalid validation status.")
        _require(_integer(payload.get("item_count")) if payload["status"] == "valid"
                 else payload.get("item_count") is None, "Invalid validated item count.")
        content = finishes[-1]["content"]
        batch = next(
            row["batch_index"] for row in starts
            if row["call_id"] == payload["call_id"]
        )
        if payload["status"] == "empty_body":
            _require(not content.strip(), "Nonempty MCQ response marked empty.")
        else:
            try:
                reconstructed = _reconstruct_mcq_content(content, planned[batch])
            except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
                _require(payload["status"] == "invalid",
                         "Invalid raw MCQ response marked valid.")
            else:
                _require(payload["status"] == "valid"
                         and payload["item_count"] == len(reconstructed.items),
                         "Valid raw MCQ response marked invalid or miscounted.")
        return

    if kind == "citation_processed":
        batch = payload.get("batch_index")
        call_id = payload.get("call_id")
        valid = next((row for row in validations if row["call_id"] == call_id
                      and row["status"] == "valid"), None)
        _require(batch in planned and valid is not None, "Citation before valid result.")
        start = next((row for row in starts if row["call_id"] == call_id), None)
        _require(start is not None and start.get("batch_index") == batch,
                 "Citation batch/call mismatch.")
        _require(payload.get("action") in {"keep", "drop"}, "Invalid MCQ citation action.")
        prior_for_call = [
            row for row in rows("citation_processed") if row["call_id"] == call_id
        ]
        ordinal = len(prior_for_call) + 1
        _require(payload.get("original_index") == ordinal <= valid["item_count"],
                 "Invalid item ordinal.")
        _require(payload.get("item_id") == f"{call_id}-item-{ordinal}",
                 "Invalid MCQ item identity.")
        _require(_valid_mcq_item(payload.get("pre_policy")), "Invalid normalized MCQ item.")
        raw_content = next(
            row["content"] for row in finishes if row["call_id"] == call_id
        )
        reconstructed = _reconstruct_mcq_content(raw_content, planned[batch])
        _require(
            payload["pre_policy"]
            == reconstructed.items[ordinal - 1].model_dump(),
            "Citation item is not derived from the raw MCQ response.",
        )
        if payload["action"] == "keep":
            _require(payload.get("post_policy") == payload["pre_policy"]
                     and _integer(payload.get("final_index"), 1), "Keep changed MCQ item.")
        else:
            _require(payload.get("post_policy") is None and payload.get("final_index") is None,
                     "Dropped MCQ item was retained.")
        checks = payload.get("option_checks")
        _require(isinstance(checks, dict) and set(checks) == set("ABCD"),
                 "Missing option citation checks.")
        from overfit.evaluation.citations import check_citation

        batch_materials = next(
            row["chunks"] for row in rows("materials_selected")
            if row["batch_index"] == batch
        )
        recomputed = {
            label: check_citation(option, batch_materials)
            for label, option in payload["pre_policy"]["options"].items()
        }
        allowed_pages = {}
        for chunk in batch_materials:
            allowed_pages.setdefault(chunk["source"], set()).update(
                range(chunk["page"], (chunk.get("page_end") or chunk["page"]) + 1)
            )
        all_valid = all(
            option["source"] in allowed_pages
            and set(range(option["page"], (option.get("page_end") or option["page"]) + 1))
            <= allowed_pages[option["source"]]
            for option in payload["pre_policy"]["options"].values()
        )
        _require(checks == recomputed, "Option citation checks do not match materials.")
        _require((payload["action"] == "keep") is all_valid,
                 "MCQ citation action conflicts with option checks.")
        kept_for_call = sum(
            row["call_id"] == call_id and row["action"] == "keep"
            for row in rows("citation_processed")
        )
        if payload["action"] == "keep":
            _require(payload["final_index"] == kept_for_call + 1,
                     "Invalid retained MCQ item index.")
        return

    if kind == "deduplication_finished":
        _require(not pending and not rows(kind), "Invalid deduplication order.")
        _require(all(any(marker["batch_index"] == batch and marker["state"] == "completed"
                         for marker in rows("batch_checkpoint")) for batch in planned),
                 "Deduplication before all batches completed.")
        _require(isinstance(payload.get("items"), list)
                 and all(_valid_mcq_item(item) for item in payload["items"])
                 and isinstance(payload.get("removed"), list), "Invalid deduplication result.")
        from overfit.models import GeneratedMCQItem
        from overfit.query.mcq import deduplicate_items

        kept = [
            row["post_policy"] for row in rows("citation_processed")
            if row["post_policy"] is not None
        ]
        actual = deduplicate_items(
            [GeneratedMCQItem.model_validate(item) for item in kept]
        )
        _require(
            [item.model_dump() for item in actual.items] == payload["items"]
            and actual.removed == payload["removed"],
            "Deduplication result is not derived from processed items.",
        )
        return

    if kind == "options_balanced":
        _require(len(rows("deduplication_finished")) == 1 and not rows(kind),
                 "Invalid balance order.")
        _require(payload.get("seed_identity") == manifest["task"]["seed_identity"],
                 "Balance seed mismatch.")
        _require(isinstance(payload.get("items"), list)
                 and all(_valid_mcq_item(item) for item in payload["items"])
                 and isinstance(payload.get("mappings"), list), "Invalid balance result.")
        from overfit.models import GeneratedMCQItem
        from overfit.query.mcq import balance_options, deduplicate_items, mix_items

        dedup_items = [
            GeneratedMCQItem.model_validate(item)
            for item in rows("deduplication_finished")[0]["items"]
        ]
        expected = balance_options(
            dedup_items, seed_identity=manifest["task"]["seed_identity"]
        )
        expected_balanced = [item.model_dump() for item in expected.items]
        expected_items = [
            item.model_dump()
            for item in mix_items(
                expected.items, seed_identity=manifest["task"]["seed_identity"]
            )
        ]
        order = [expected_balanced.index(item) for item in expected_items]
        citation_rows = [
            row for row in rows("citation_processed")
            if row["post_policy"] is not None
        ]
        source_items = [
            GeneratedMCQItem.model_validate(row["post_policy"])
            for row in citation_rows
        ]
        origin_by_id = {
            id(item): row["batch_index"]
            for item, row in zip(source_items, citation_rows, strict=True)
        }
        replay_dedup = deduplicate_items(source_items)
        origins = [origin_by_id[id(item)] for item in replay_dedup.items]
        _require(
            payload.get("balanced_items") == expected_balanced
            and payload.get("mappings") == expected.mappings
            and payload.get("items") == expected_items
            and payload.get("presentation_order") == order
            and payload.get("item_material_batches")
            == [origins[index] for index in order],
            "Option balance/mix is not derived atomically from deduplicated items.",
        )
        return

    if kind == "run_finished":
        _require(not pending and payload.get("status") in {
            "completed", "empty", "failed", "interrupted"
        }, "Invalid terminal state.")
        artifacts = payload.get("artifacts")
        _require(isinstance(artifacts, dict) and "manifest.json" in artifacts,
                 "Missing manifest integrity record.")
        if payload["status"] in {"completed", "empty"}:
            _require(payload.get("stage") == "complete" and payload.get("error") is None,
                     "Invalid successful MCQ terminal metadata.")
            _require(len(rows("deduplication_finished")) == len(rows("options_balanced")) == 1,
                     "Successful MCQ run lacks finalization events.")
            _require({"result.json", "report.md"} <= artifacts.keys(), "Missing result artifacts.")
            if payload["status"] == "completed":
                _require(any(name.endswith("_mock_exam.md") for name in artifacts)
                         and any(name.endswith("_answers.md") for name in artifacts),
                         "Missing exam artifacts.")
            computed = summarize(manifest, accepted)
            _require(payload.get("generation_outcome") == computed["generation_outcome"],
                     "MCQ generation outcome does not match events.")
            _require(
                (payload["status"] == "completed" and computed["retained_count"] > 0)
                or (payload["status"] == "empty" and computed["retained_count"] == 0),
                "MCQ terminal status conflicts with retained items.",
            )
        else:
            safe_errors = {
                "interrupted": "Operation interrupted.",
                "trace_write_error": "Trace recording failed.",
                "timeout": "Operation timed out.",
                "io_error": "An I/O operation failed.",
                "validation_error": "Data validation failed.",
                "operation_error": "Operation failed.",
            }
            error = payload.get("error")
            _require(
                isinstance(error, dict)
                and set(error) == {"type", "message"}
                and error.get("type") in safe_errors
                and error.get("message") == safe_errors[error["type"]],
                "Invalid failed MCQ terminal error.",
            )
            _require(
                (payload["status"] == "interrupted")
                is (error["type"] == "interrupted"),
                "MCQ terminal status conflicts with its safe error.",
            )
            artifacts = payload.get("artifacts")
            has_result_artifacts = isinstance(artifacts, dict) and any(
                name != "manifest.json" and not name.startswith("checkpoint-batch-")
                for name in artifacts
            )
            has_generation_events = any(
                rows(name)
                for name in (
                    "batch_checkpoint", "materials_selected", "call_started",
                    "call_finished", "validation_finished", "citation_processed",
                    "deduplication_finished", "options_balanced",
                )
            )
            allowed_stages = (
                {"artifacts"} if has_result_artifacts
                else {"generation", "artifacts"} if has_generation_events
                else {"initialization", "index", "selection"}
            )
            _require(payload.get("stage") in allowed_stages,
                     "Failed MCQ stage conflicts with observed lifecycle.")
            computed = summarize(manifest, accepted)
            declared_generation_failure = (
                payload["status"] == "failed"
                and payload["stage"] == "generation"
                and bool(starts)
                and not any(row["status"] == "valid" for row in validations)
                and payload.get("generation_outcome") == "failed"
            )
            _require(
                declared_generation_failure
                or payload.get("generation_outcome") == computed["generation_outcome"],
                "Failed MCQ generation outcome conflicts with observed events.",
            )
        finished_ids = {row["call_id"] for row in finishes}
        expected_counts = {
            "call_started": len(starts),
            "call_finished": len(finishes),
            "unresolved": sum(row["call_id"] not in finished_ids for row in starts),
            "call_count": len(starts) if all(
                row["call_id"] in finished_ids for row in starts
            ) else None,
        }
        _require(payload.get("counts") == expected_counts,
                 "MCQ terminal call counts do not match events.")
        _require(all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                     for value in artifacts.values()), "Invalid artifact digest.")
        return

    raise ValueError("Unsupported MCQ event.")


def _require_declared_version(raw) -> None:
    """Readers never apply a writer's default version to an undeclared artifact."""
    def unique(pairs):
        value = {}
        for key, item in pairs:
            _require(key not in value, "Duplicate recording key.")
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique)
    _require(isinstance(value, dict) and type(value.get("schema_version")) is int
             and value["schema_version"] in {1, 2}, "Missing or unsupported declared version.")


def inspect_run(run_dir: Path) -> dict:
    """Return validated-prefix diagnostics; unreadable/unknown manifests raise."""
    root = Path(run_dir).expanduser().resolve()
    raw_manifest = _read(root, "manifest.json")
    try:
        _require_declared_version(raw_manifest)
        manifest = Manifest.model_validate_json(raw_manifest).model_dump(mode="json")
        _require(_integer(manifest["task"]["questions"], 1), "Invalid requested count.")
    except (ValueError, KeyError, TypeError, ValidationError):
        raise TraceReadError("Invalid or unsupported manifest.") from None
    try:
        raw_log = _read(root, "traces.jsonl")
    except TraceReadError:
        raw_log = None
    events, problems = [], []
    if raw_log is None:
        problems.append("Event log is missing or unreadable.")
    else:
        for number, line in enumerate(raw_log.splitlines(keepends=True), 1):
            try:
                _require(line.endswith(b"\n"), "Truncated event line.")
                _require_declared_version(line)
                event = Event.model_validate_json(line).model_dump(mode="json")
                _validate_event(event, events, manifest)
            except (ValueError, KeyError, TypeError, ValidationError):
                problems.append(f"Invalid event at line {number}; subsequent events not accepted.")
                break
            events.append(event)
    terminal = next((e["payload"] for e in reversed(events)
                     if e["event_type"] == "run_finished"), None)
    if terminal is None:
        problems.append("No valid terminal event; original run status is unknown.")
    else:
        for name, digest in terminal["artifacts"].items():
            try:
                _require(_digest(_read(root, name)) == digest, "Artifact digest mismatch.")
            except (ValueError, TraceReadError):
                problems.append("A declared artifact is missing, unsafe, or has a digest mismatch.")
    if (
        terminal is not None
        and not problems
        and manifest.get("task", {}).get("generation_contract") == "mcq-v1"
        and terminal["status"] in {"completed", "empty"}
    ):
        try:
            summary = summarize(manifest, events)
            expected_result = (
                json.dumps(
                    summary,
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            ).encode()
            _require(_read(root, "result.json") == expected_result,
                     "MCQ result artifact differs from replay.")
            _require(_read(root, "report.md") == render_report(summary).encode(),
                     "MCQ report artifact differs from replay.")
            paper_names = [
                name for name in terminal["artifacts"]
                if name.endswith("_mock_exam.md")
            ]
            answer_names = [
                name for name in terminal["artifacts"]
                if name.endswith("_answers.md")
            ]
            if terminal["status"] == "completed":
                _require(len(paper_names) == len(answer_names) == 1,
                         "MCQ artifact set is ambiguous.")
                from overfit.models import GeneratedMCQExam
                from overfit.query.generator import render_exam

                exam = GeneratedMCQExam.model_validate({"items": summary["items"]})
                paper, answers = render_exam(
                    manifest["task"]["course"],
                    exam,
                    answers_file=answer_names[0],
                    generated_on=manifest["started_at"][:10],
                )
                _require(_read(root, paper_names[0]) == paper.encode()
                         and _read(root, answer_names[0]) == answers.encode(),
                         "MCQ exam artifacts differ from replay.")
            else:
                _require(not paper_names and not answer_names,
                         "Empty MCQ run published exam artifacts.")
        except (ValueError, TypeError, KeyError, ValidationError, TraceReadError):
            problems.append("MCQ artifacts do not match reconstructed events.")
    result = summarize(manifest, events)
    result.update({
        "run_status": terminal["status"] if terminal else "unknown",
        "trace_integrity": "incomplete" if problems else "complete",
        "original_cli_exit_status": None,
        "count_scope": "validated_prefix" if problems else "complete_recording",
        "failure_stage": (terminal["stage"] if terminal
                          and terminal["status"] in {"failed", "interrupted"} else None),
        "error": terminal.get("error") if terminal else None,
        "last_valid_seq": len(events), "problems": problems,
        "manifest_sha256": _digest(raw_manifest),
        "trace_sha256": _digest(raw_log) if raw_log is not None else None,
    })
    return result


def replay_run(run_dir: Path) -> dict:
    result = inspect_run(run_dir)
    root = Path(run_dir).expanduser().resolve()
    parent = root / "replays"
    if parent.is_symlink():
        raise TraceWriteError("Replay output must not be a symlink.")
    try:
        parent.mkdir(exist_ok=True, mode=0o700)
        for _ in range(10):
            replay_id = str(uuid4())
            target = parent / replay_id
            try:
                target.mkdir(mode=0o700)
                break
            except FileExistsError:
                continue
        else:
            raise OSError("Cannot allocate replay directory")
        result.update({"replay_id": replay_id, "replayed_at": datetime.now(UTC).isoformat()})
        for name, content in (
            ("result.json", json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n"),
            ("report.md", render_report(result)),
        ):
            with (target / name).open("x", encoding="utf-8") as out:
                os.chmod(target / name, 0o600)
                out.write(content)
                out.flush()
                os.fsync(out.fileno())
    except (OSError, ValueError):
        raise TraceWriteError("Could not write replay output.") from None
    return {**result, "replay_dir": str(target)}
