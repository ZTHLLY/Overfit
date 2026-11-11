"""Current MCQ writes and strict offline replay, isolated from frozen v1 policy."""

from __future__ import annotations

import math
import re
from copy import deepcopy
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import ValidationError

from overfit.evaluation import judge
from overfit.evaluation import mcq_protocols as protocol
from overfit.evaluation import runner as r
from overfit.evaluation.trace import safe_error

VERSION = "mcq-judge-v2"
CAPABILITY_GENERATION_RUN_ID = "e7b0d033-b90e-4399-a8ab-0232635410e2"
CAPABILITY_ITEMS = (3, 5)
TASK_FIELDS = {
    "schema_version", "generation_contract", "source", "course", "chunks",
    "item_materials", "items", "generation_model",
}
MANIFEST_FIELDS = {
    "schema_version", "judgment_id", "mode", "created_at", "protocol_version",
    "protocol_digest", "source_identity", "judge_config", "max_calls",
    "selected_items", "generation_contract", "workflow", "publication_policy",
    "base_identity",
}
USAGE_FIELDS = {
    "prompt_tokens", "completion_tokens", "reasoning_tokens",
    "visible_completion_tokens", "total_tokens",
}
SAFE_ERRORS = {
    "interrupted": "Operation interrupted.", "trace_write_error": "Trace recording failed.",
    "timeout": "Operation timed out.", "io_error": "An I/O operation failed.",
    "validation_error": "Data validation failed.", "operation_error": "Operation failed.",
}


def _path(value):
    """Reject symlinks before resolve can hide them (including ancestor links)."""
    path = Path(value).expanduser().absolute()
    r._check(not any(parent.is_symlink() for parent in (path, *path.parents)),
             "Symlink paths are not supported.")
    return path.resolve()


def parse_items(value, count):
    """Explicit one-based strictly increasing indices; no coercion or defaults."""
    if isinstance(value, str):
        r._check(bool(re.fullmatch(r"[1-9][0-9]*(,[1-9][0-9]*)*", value)),
                 "Items must be explicit sorted unique one-based integers.")
        value = [int(part) for part in value.split(",")]
    r._check(type(value) is list and bool(value)
             and all(type(index) is int and 1 <= index <= count for index in value)
             and all(a < b for a, b in pairwise(value)),
             "Items must be explicit sorted unique indices within the generation run.")
    return list(value)


def _stamp(value):
    r._check(type(value) is str)
    stamp = datetime.fromisoformat(value)
    r._check(stamp.utcoffset() is not None and stamp.utcoffset().total_seconds() == 0,
             "Recording timestamps must be UTC.")
    return stamp


def _lines(raw):
    r._check(not raw or raw.endswith(b"\n"), "Truncated JSONL recording.")
    return [r._json(line) for line in raw.splitlines()]


def _config(value):
    """Validate recorded safe runtime identity without settings/environment access."""
    r._check(type(value) is dict and set(value) == {
        "base_url", "model", "timeout", "max_input_chars",
    }, "Invalid v2 runtime configuration envelope.")
    r._check(type(value["base_url"]) is str and type(value["model"]) is str
             and bool(value["model"].strip()) and type(value["timeout"]) is float
             and math.isfinite(value["timeout"]) and value["timeout"] > 0
             and type(value["max_input_chars"]) is int and value["max_input_chars"] > 0,
             "Invalid v2 runtime configuration types.")
    parsed = urlsplit(value["base_url"])
    r._check(value["base_url"] == value["base_url"].strip()
             and not any(char.isspace() for char in value["base_url"])
             and parsed.scheme in {"http", "https"} and bool(parsed.hostname)
             and parsed.username is None and parsed.password is None
             and not parsed.query and not parsed.fragment,
             "Unsafe v2 endpoint.")
    _ = parsed.port
    return SimpleNamespace(**value)


def _base(root, source_root, tasks):
    """Fully verify frozen v1 evidence, then bind every artifact without modifying it."""
    root = _path(root)
    r._check(root.parent == source_root / "judgments", "Base judgment must belong to this generation run.")
    manifest = r._json(r._read(root, "manifest.json"))
    r._check(manifest.get("protocol_version") == "mcq-judge-v1",
             "Calibration requires original v1 comparison evidence, not resume-of-resume.")
    r._check(r._same_json(manifest.get("source_identity"), tasks["source"]),
             "Base and generation identities differ.")
    r._replay_mcq_judgment(root, verify_only=True)
    complete_raw = r._read(root, "complete.json")
    complete = r._json(complete_raw)
    return {
        "judgment_id": root.name, "protocol_version": "mcq-judge-v1",
        "complete_sha256": r.digest(complete_raw), "artifacts": complete["artifacts"],
    }


def _request(item, chunks, settings, attempt):
    return protocol.build_mcq_request(item, chunks, settings,
                                      protocol_version=VERSION, attempt=attempt)


def _classify(finish, item):
    if finish["status"] != "response":
        result = "interrupted" if finish["status"] == "interrupted" else "transport_error"
        checked = None
    else:
        response = finish["response"]
        result = response["finish_reason"]
        checked = None
        if result == "stop":
            try:
                checked = protocol.validate_mcq_review(response["content"], item,
                                                        protocol_version=VERSION)
                result = "valid"
            except judge.JudgeOutputError:
                result = "schema_error"
        elif result not in {"refusal", "length"}:
            result = "transport_error"
    disposition = protocol.attempt_disposition(
        VERSION, attempt=finish["attempt"], result=result,
        retryable=finish["retryable"],
    )
    if checked is not None:
        row = {"item": item, "status": "evaluated", "reason": None, **checked}
    else:
        reason = {"length": "truncated_response"}.get(result, result)
        row = {"item": item, "status": "error", "reason": reason}
    return disposition, row


def _validate_finish(event):
    r._check(event["status"] in {"response", "error", "interrupted"}
             and type(event["retryable"]) is bool
             and (event["status"] == "error" or event["retryable"] is False)
             and type(event["elapsed_seconds"]) is float
             and math.isfinite(event["elapsed_seconds"]) and event["elapsed_seconds"] >= 0)
    if event["status"] == "response":
        response = event["response"]
        r._check(type(response) is dict
                 and set(response) == {"content", "usage", "finish_reason", "model"}
                 and type(response["content"]) is str
                 and (response["finish_reason"] is None or type(response["finish_reason"]) is str)
                 and (response["model"] is None or type(response["model"]) is str)
                 and event["error"] is None and event["retryable"] is False,
                 "Unsafe v2 response envelope.")
        usage = response["usage"]
        r._check(usage is None or (type(usage) is dict and bool(usage)
                 and set(usage) <= USAGE_FIELDS
                 and all((type(value) is int and value >= 0)
                         or (type(value) is float and math.isfinite(value) and value >= 0)
                         for value in usage.values())), "Unsafe v2 usage.")
        if usage:
            derivable = ("completion_tokens" in usage and "reasoning_tokens" in usage
                         and usage["completion_tokens"] >= usage["reasoning_tokens"])
            r._check(("visible_completion_tokens" in usage) is derivable,
                     "Visible usage must follow frozen normalization rules.")
            if derivable:
                r._check(r._same_json(usage["visible_completion_tokens"],
                                      usage["completion_tokens"] - usage["reasoning_tokens"]),
                         "Visible usage is not derived from reasoning usage.")
    else:
        error = event["error"]
        r._check(event["response"] is None and type(error) is dict
                 and set(error) == {"type", "message"}
                 and error.get("type") in SAFE_ERRORS
                 and error["message"] == SAFE_ERRORS[error["type"]]
                 and ((event["status"] == "interrupted") is (error["type"] == "interrupted")),
                 "Unsafe v2 error envelope.")


def _derive(tasks, manifest, events):
    """One schedule verifier reconstructs rows for both writer and replay."""
    selected = manifest["selected_items"]
    executing = manifest["mode"] == "execute"
    settings = _config(manifest["judge_config"]) if executing else None
    rows, position, interrupted = [], 0, False
    ids = set()
    previous_stamp = _stamp(manifest["created_at"])
    for index, item in enumerate(tasks["items"], 1):
        if index not in selected:
            rows.append({"item": item, "status": "not_evaluated", "reason": "not_selected"})
            continue
        if not executing or interrupted:
            rows.append({"item": item, "status": "not_evaluated",
                         "reason": "interrupted" if interrupted else "prepare"})
            continue
        chunks = tasks["item_materials"][index - 1]
        if r._request_size(_request(item, chunks, settings, 1)) > settings.max_input_chars:
            rows.append({"item": item, "status": "error", "reason": "input_limit"})
            continue
        for attempt in (1, 2):
            r._check(position + 2 <= len(events), "Missing required v2 call or retry.")
            start, finish = events[position:position + 2]
            common = {"schema_version", "judgment_id", "seq", "time", "type", "call_id",
                      "item_index", "attempt"}
            for offset, event in enumerate((start, finish)):
                expected = common | ({"request"} if offset == 0 else {
                    "elapsed_seconds", "status", "response", "error", "retryable",
                })
                r._check(type(event) is dict and set(event) == expected
                         and type(event["schema_version"]) is int and event["schema_version"] == 4
                         and event["judgment_id"] == manifest["judgment_id"]
                         and type(event["seq"]) is int and event["seq"] == position + offset + 1
                         and type(event["item_index"]) is int and event["item_index"] == index
                         and type(event["attempt"]) is int and event["attempt"] == attempt
                         and type(event["call_id"]) is str and bool(event["call_id"]),
                         "Invalid v2 event identity or schedule.")
                stamp = _stamp(event["time"])
                r._check(stamp >= previous_stamp, "V2 event time regressed.")
                previous_stamp = stamp
            r._check(start["type"] == "call_started" and finish["type"] == "call_finished"
                     and start["call_id"] == finish["call_id"] and start["call_id"] not in ids,
                     "Invalid v2 call identity.")
            ids.add(start["call_id"])
            r._check(r._same_json(start["request"], _request(item, chunks, settings, attempt)),
                     "V2 request differs from frozen protocol.")
            _validate_finish(finish)
            disposition, row = _classify(finish, item)
            position += 2
            if disposition != "retry":
                interrupted = disposition == "interrupted"
                rows.append(row)
                break
        else:
            r._check(False, "Retry exceeded the frozen attempt schedule.")
    r._check(position == len(events) and len(ids) <= manifest["max_calls"],
             "Extra v2 calls after terminal result or outside selected items.")
    selected_rows = [rows[index - 1] for index in selected]
    status = "interrupted" if interrupted else "prepared" if not executing else (
        "completed" if all(row["status"] == "evaluated" for row in selected_rows) else "incomplete"
    )
    return rows, status


def _metrics(tasks, manifest, rows, events, status):
    selected = [rows[index - 1] for index in manifest["selected_items"]]
    positives = sum(row.get("mandatory_pass") is True and row.get("human_review") == "not_required"
                    for row in selected)
    usage = {}
    for event in events:
        if event["type"] == "call_finished" and event["status"] == "response":
            for key, value in (event["response"]["usage"] or {}).items():
                try:
                    total = usage.get(key, 0) + value
                except OverflowError:
                    raise r.JudgeRunError("Recorded usage totals exceed finite numeric limits.") from None
                r._check(type(total) is int or math.isfinite(total),
                         "Recorded usage totals exceed finite numeric limits.")
                usage[key] = total
    publish = status == "completed" and manifest["workflow"] == "formal"
    return {
        "schema_version": 2, "protocol_version": VERSION, "workflow": manifest["workflow"],
        "mode": manifest["mode"], "run_status": status, "retained_count": len(rows),
        "accepted_count": positives if publish else 0,
        "selected_evaluated_phase_count": sum(row["status"] == "evaluated" for row in selected),
        "selected_phase_count": len(selected),
        "evaluated_phase_count": sum(row["status"] == "evaluated" for row in rows),
        "planned_phase_count": len(tasks["items"]), "model_all_positive_count": positives,
        "human_review_pending_item_count": sum(row.get("human_review") == "pending" for row in selected),
        "mandatory_failure_item_count": sum(row.get("mandatory_pass") is False for row in selected),
        "technical_error_phase_count": sum(row["status"] == "error" for row in selected),
        "call_starts": len(events) // 2,
        "response_count": sum(event["type"] == "call_finished" and event["status"] == "response" for event in events),
        "retry_count": sum(event["type"] == "call_started" and event["attempt"] > 1 for event in events),
        "max_calls": manifest["max_calls"], "usage": usage,
    }


def _report(metrics, rows):
    publication = "no publication" if metrics["workflow"] == "calibration" else (
        "formal complete" if metrics["run_status"] == "completed" else "no publication"
    )
    lines = ["# MCQ Judge v2 report", "", f"- Workflow: **{metrics['workflow']}**",
             f"- Status: **{metrics['run_status']}**", f"- Publication: **{publication}**",
             f"- Retained: {metrics['retained_count']}",
             f"- Accepted for judged publication: {metrics['accepted_count']}",
             f"- Mandatory failures: {metrics['mandatory_failure_item_count']}",
             f"- Pending human review: {metrics['human_review_pending_item_count']}",
             f"- Technical errors: {metrics['technical_error_phase_count']}",
             f"- Calls: {metrics['call_starts']}/{metrics['max_calls']}"]
    for name in sorted(metrics["usage"]):
        lines.append(f"- {name}: {metrics['usage'][name]}")
    for index, row in enumerate(rows, 1):
        lines.extend(["", f"## Item {index}", "", f"- Status: `{row['status']}`",
                      f"- Mandatory pass: `{row.get('mandatory_pass')}`",
                      f"- Human review: `{row.get('human_review')}`"])
    return "\n".join(lines) + "\n"


def _queue(rows):
    return "".join(r.canonical({"item_index": index, "status": "pending"}) + "\n"
                   for index, row in enumerate(rows, 1) if row.get("human_review") == "pending")


def _publication(tasks, manifest, rows):
    from overfit.models import GeneratedMCQExam, GeneratedMCQItem
    from overfit.query.generator import render_exam
    from overfit.query.mcq import balance_options, mix_items
    accepted = [GeneratedMCQItem.model_validate(row["item"]) for row in rows
                if row.get("mandatory_pass") is True and row.get("human_review") == "not_required"]
    seed = r.digest(f"{tasks['source']['run_id']}:{manifest['judgment_id']}:judged".encode())
    balanced = balance_options(accepted, seed_identity=seed)
    items = mix_items(balanced.items, seed_identity=seed)
    paper, answers = render_exam(tasks["course"], GeneratedMCQExam(items=items),
                                 answers_file="judged_answers.md", generated_on=manifest["created_at"][:10])
    return {"judged_mock_exam.md": paper, "judged_answers.md": answers}, items, seed


def _verify_bound_source(source_root, tasks, base_root, base):
    """Revalidate snapshots both before calls and after the final provider response."""
    _, _, current = r._mcq_source(source_root)
    r._check(r._same_json(current, tasks), "Source changed during Judge execution.")
    if base is not None:
        r._check(r._same_json(_base(base_root, source_root, tasks), base),
                 "Base comparison evidence changed during Judge execution.")


def _run(run_dir, *, workflow, items=None, base_judgment=None, execute=False,
         max_items=None, settings=None, client_factory=None, expected_source=None):
    source_root = _path(run_dir)
    source_root, _, tasks = r._mcq_source(source_root)
    tasks = deepcopy(tasks)
    if expected_source is not None:
        r._check(r._same_json(tasks["source"], expected_source), "Capability source identity changed.")
    count = len(tasks["items"])
    if workflow == "formal":
        r._check(max_items is None or (r._positive(max_items) and max_items >= count),
                 "Formal v2 Judge requires every retained item; use explicit judge-review for calibration.")
        selected = list(range(1, count + 1))
        base = None
    else:
        selected = parse_items(items, count)
        base = _base(base_judgment, source_root, tasks)
    if execute:
        try:
            if settings is None:
                settings = protocol.MCQJudgeV2Settings()
            r._check(isinstance(settings, protocol.MCQJudgeV2Settings), "V2 requires dedicated strict settings.")
            config = protocol.v2_safe_identity(settings)
            _config(config)
        except (ValidationError, ValueError, TypeError):
            raise r.JudgeRunError("Invalid v2 Judge shared runtime configuration.") from None
    else:
        config = None
    target = r._unique(source_root / "judgments")
    record = r.Recording(target)
    manifest = {
        "schema_version": 2, "judgment_id": target.name,
        "mode": "execute" if execute else "prepare", "created_at": datetime.now(UTC).isoformat(),
        "protocol_version": VERSION, "protocol_digest": protocol.mcq_protocol_digest(VERSION),
        "source_identity": tasks["source"], "judge_config": config, "max_calls": len(selected) * 2,
        "selected_items": selected, "generation_contract": "mcq-v1", "workflow": workflow,
        "publication_policy": "no publication" if workflow == "calibration" else "full v2 only",
        "base_identity": base,
    }
    record.write("manifest.json", r.canonical(manifest) + "\n")
    record.write("tasks.json", r.canonical(tasks) + "\n")
    record.write("calls.jsonl", "")
    client, interrupted = None, False
    if execute:
        for index in selected:
            item, chunks = tasks["items"][index - 1], tasks["item_materials"][index - 1]
            if r._request_size(_request(item, chunks, settings, 1)) > settings.max_input_chars:
                continue
            for attempt in (1, 2):
                # Recheck frozen sources immediately before constructing/calling the provider.
                _verify_bound_source(source_root, tasks, base_judgment, base)
                if client is None:
                    try:
                        client = (client_factory or judge.make_client)(settings)
                    except Exception:  # noqa: BLE001 - do not expose credentials or SDK internals
                        raise r.JudgeRunError("Judge client initialization failed.") from None
                request = _request(item, chunks, settings, attempt)
                call_id = str(uuid4())
                record.emit("call_started", call_id=call_id, item_index=index, attempt=attempt, request=request)
                started = monotonic()
                try:
                    response = protocol.invoke_mcq_v2(client, deepcopy(request))
                    finish = {"status": "response", "response": response, "error": None, "retryable": False}
                except KeyboardInterrupt as exc:
                    finish = {"status": "interrupted", "response": None, "error": safe_error(exc), "retryable": False}
                except Exception as exc:  # noqa: BLE001 - safe classified provider boundary
                    finish = {"status": "error", "response": None, "error": safe_error(exc), "retryable": r._transient(exc)}
                finish["elapsed_seconds"] = float(monotonic() - started)
                _validate_finish(finish)
                record.emit("call_finished", call_id=call_id, item_index=index, attempt=attempt, **finish)
                disposition, _ = _classify({**finish, "attempt": attempt}, item)
                if disposition != "retry":
                    interrupted = disposition == "interrupted"
                    break
            if interrupted:
                break
    _verify_bound_source(source_root, tasks, base_judgment, base)
    rows, status = _derive(tasks, manifest, record.events)
    metrics = _metrics(tasks, manifest, rows, record.events, status)
    publication, accepted = None, []
    try:
        if status == "completed" and workflow == "formal":
            artifacts, models, seed = _publication(tasks, manifest, rows)
            for name, text in artifacts.items():
                record.write(name, text)
            accepted = [item.model_dump() for item in models]
            publication = {"paper": str(target / "judged_mock_exam.md"),
                           "answers": str(target / "judged_answers.md"), "seed_identity": seed}
        record.write("judgments.jsonl", "".join(r.canonical(row) + "\n" for row in rows))
        record.write("review_queue.jsonl", _queue(rows))
        record.write("metrics.json", r.canonical(metrics) + "\n")
        record.write("report.md", _report(metrics, rows))
        names = r.ARTIFACTS | (r.MCQ_PUBLICATION_ARTIFACTS if publication else set())
        record.write("complete.json", r.canonical({
            "schema_version": 2, "judgment_id": target.name, "status": status,
            "artifacts": {name: r.digest(r._read(target, name)) for name in sorted(names)},
        }) + "\n")
    except (Exception, KeyboardInterrupt):
        # Publication is committed only by complete.json. Remove partial public
        # papers if persistence failed, while preserving every raw call as evidence.
        for name in r.MCQ_PUBLICATION_ARTIFACTS:
            (target / name).unlink(missing_ok=True)
        raise
    return {"judgment_dir": str(target), "report_path": str(target / "report.md"),
            "status": status, "workflow": workflow, "protocol_version": VERSION,
            "metrics": metrics, "publication": publication, "accepted_items": accepted,
            "exit_code": 130 if interrupted else 1 if status == "incomplete" else 0}


def run_formal(run_dir, **kwargs):
    return _run(run_dir, workflow="formal", **kwargs)


def run_calibration(run_dir, *, base_judgment, items, **kwargs):
    return _run(run_dir, workflow="calibration", base_judgment=base_judgment, items=items, **kwargs)


def run_capability(run_dir, *, base_judgment, items, **kwargs):
    """Explicit P9 lane: bind the frozen source identity and exact 3,5 selection."""
    source_root = _path(run_dir)
    _, _, tasks = r._mcq_source(source_root)
    selected = parse_items(items, len(tasks["items"]))
    r._check(tasks["source"]["run_id"] == CAPABILITY_GENERATION_RUN_ID
             and selected == list(CAPABILITY_ITEMS),
             "Capability gate requires frozen generation identity and items 3,5.")
    return _run(source_root, workflow="calibration", base_judgment=base_judgment,
                items=selected, expected_source=tasks["source"], **kwargs)


def _reconstruct(root):
    root = _path(root)
    complete = r._json(r._read(root, "complete.json"))
    r._check(type(complete) is dict and set(complete) == {"schema_version", "judgment_id", "status", "artifacts"}
             and type(complete["schema_version"]) is int and complete["schema_version"] == 2
             and complete["judgment_id"] == root.name
             and complete["status"] in {"prepared", "completed", "incomplete", "interrupted"})
    r._check(type(complete["artifacts"]) is dict)
    raw = {name: r._read(root, name) for name in complete["artifacts"]}
    r._check(all(type(value) is str and r.digest(raw[name]) == value
                 for name, value in complete["artifacts"].items()), "V2 artifact hashes do not match.")
    r._check(r.ARTIFACTS <= set(raw))
    manifest, tasks = r._json(raw["manifest.json"]), r._json(raw["tasks.json"])
    r._check(type(manifest) is dict and set(manifest) == MANIFEST_FIELDS
             and type(manifest["schema_version"]) is int and manifest["schema_version"] == 2
             and manifest["judgment_id"] == root.name and manifest["protocol_version"] == VERSION
             and manifest["protocol_digest"] == protocol.mcq_protocol_digest(VERSION)
             and manifest["workflow"] in {"formal", "calibration"}
             and manifest["mode"] in {"prepare", "execute"}, "Invalid v2 manifest or protocol digest.")
    _stamp(manifest["created_at"])
    r._check(type(tasks) is dict and set(tasks) == TASK_FIELDS
             and type(tasks["schema_version"]) is int and tasks["schema_version"] == 1
             and manifest["generation_contract"] == tasks["generation_contract"] == "mcq-v1"
             and r._same_json(manifest["source_identity"], tasks["source"]))
    source_root = root.parent.parent
    _, _, actual = r._mcq_source(source_root)
    r._check(r._same_json(actual, tasks), "V2 source snapshot no longer matches generation.")
    selected = manifest["selected_items"]
    if manifest["workflow"] == "formal":
        r._check(type(selected) is list and r._same_json(selected, list(range(1, len(tasks["items"]) + 1))))
        r._check(manifest["base_identity"] is None and manifest["publication_policy"] == "full v2 only")
    else:
        parse_items(selected, len(tasks["items"]))
        base = manifest["base_identity"]
        r._check(type(base) is dict and set(base) == {"judgment_id", "protocol_version", "complete_sha256", "artifacts"}
                 and type(base["judgment_id"]) is str and bool(re.fullmatch(r"[A-Za-z0-9-]+", base["judgment_id"]))
                 and manifest["publication_policy"] == "no publication")
        r._check(r._same_json(_base(source_root / "judgments" / base["judgment_id"], source_root, tasks), base),
                 "Frozen base evidence changed.")
    r._check(type(manifest["max_calls"]) is int and manifest["max_calls"] == len(selected) * 2)
    if manifest["mode"] == "prepare":
        r._check(manifest["judge_config"] is None)
    else:
        _config(manifest["judge_config"])
    rows, status = _derive(tasks, manifest, _lines(raw["calls.jsonl"]))
    r._check(status == complete["status"])
    published = status == "completed" and manifest["workflow"] == "formal"
    expected = r.ARTIFACTS | (r.MCQ_PUBLICATION_ARTIFACTS if published else set())
    r._check(set(raw) == expected, "Calibration or incomplete v2 recording cannot publish.")
    r._check(all(not (root / name).exists() and not (root / name).is_symlink()
                 for name in r.MCQ_PUBLICATION_ARTIFACTS - expected), "Unexpected publication artifacts.")
    r._check(r._same_json(_lines(raw["judgments.jsonl"]), rows), "V2 rows differ from raw calls.")
    metrics = _metrics(tasks, manifest, rows, _lines(raw["calls.jsonl"]), status)
    r._check(r._same_json(r._json(raw["metrics.json"]), metrics), "V2 metrics differ from raw calls.")
    r._check(raw["report.md"].decode() == _report(metrics, rows)
             and raw["review_queue.jsonl"].decode() == _queue(rows), "V2 report or queue differs from raw calls.")
    if published:
        artifacts, _, _ = _publication(tasks, manifest, rows)
        r._check(all(raw[name].decode() == text for name, text in artifacts.items()),
                 "V2 publication differs from accepted raw reviews.")
    return metrics, rows


def replay(root):
    try:
        root = _path(root)
        metrics, rows = _reconstruct(root)
    except r.JudgeRunError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OSError):
        raise r.JudgeRunError("Invalid or unsupported MCQ Judge v2 recording.") from None
    target = r._unique(root / "replays")
    record = r.Recording(target)
    record.write("judgments.jsonl", "".join(r.canonical(row) + "\n" for row in rows))
    record.write("review_queue.jsonl", _queue(rows))
    record.write("metrics.json", r.canonical(metrics) + "\n")
    record.write("report.md", _report(metrics, rows))
    status = metrics["run_status"]
    return {"replay_dir": str(target), "report_path": str(target / "report.md"),
            "status": status, "workflow": metrics["workflow"], "protocol_version": VERSION,
            "metrics": metrics, "exit_code": 130 if status == "interrupted" else 1 if status == "incomplete" else 0}
