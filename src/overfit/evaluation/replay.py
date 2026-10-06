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

from overfit.evaluation.citations import check_citation
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
    kind, p = event["event_type"], event["payload"]
    _require(event["run_id"] == manifest["run_id"], "Event run ID mismatch.")
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
        chunks = p["chunks"]
        _require(isinstance(chunks, list), "Invalid materials.")
        check_citation({"source": "", "page": 1}, chunks)  # validates all ranges
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


def inspect_run(run_dir: Path) -> dict:
    """Return validated-prefix diagnostics; unreadable/unknown manifests raise."""
    root = Path(run_dir).expanduser().resolve()
    raw_manifest = _read(root, "manifest.json")
    try:
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
