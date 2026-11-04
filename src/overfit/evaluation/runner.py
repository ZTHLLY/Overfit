"""Opt-in judge orchestration and offline, raw-response-based reconstruction."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

from pydantic import ValidationError

from overfit.errors import OverfitError
from overfit.evaluation import judge
from overfit.evaluation.metrics import (
    build_review_queue,
    render_judge_report,
    summarize_judgments,
)
from overfit.evaluation.replay import inspect_run
from overfit.evaluation.trace import safe_error

PHASES = ("answerability", "support")
ARTIFACTS = {"manifest.json", "tasks.json", "calls.jsonl", "judgments.jsonl", "metrics.json", "report.md", "review_queue.jsonl"}
MCQ_PUBLICATION_ARTIFACTS = {"judged_mock_exam.md", "judged_answers.md"}


class JudgeRunError(OverfitError):
    """An explicit, safe error; never embeds provider or configuration secrets."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _same_json(left, right) -> bool:
    """Compare JSON values without Python's bool/int/float equality coercions."""
    return canonical(left) == canonical(right)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _check(value, message="Invalid judge recording."):
    if not value:
        raise JudgeRunError(message)


def _checker_identity():
    base = Path(__file__).parent
    result = {name: digest((base / name).read_bytes())
              for name in ("judge.py", "runner.py", "metrics.py", "elastic.py")}
    result["provenance.py"] = digest((base.parent / "provenance.py").read_bytes())
    return result


def _positive(value):
    return type(value) is int and value > 0


def _read(root, name):
    _check(
        name
        in ARTIFACTS
        | MCQ_PUBLICATION_ARTIFACTS
        | {"complete.json", "traces.jsonl"},
        "Unsupported artifact name.",
    )
    path = root / name
    _check(not path.is_symlink(), "Artifact symlinks are not supported.")
    try:
        return path.read_bytes()
    except OSError:
        raise JudgeRunError("Required artifact is not readable.") from None


def _unique(parent):
    _check(not parent.is_symlink(), "Output directory must not be a symlink.")
    try:
        parent.mkdir(exist_ok=True, mode=0o700)
        for _ in range(10):
            target = parent / str(uuid4())
            try:
                target.mkdir(mode=0o700)
                return target
            except FileExistsError:
                continue
    except OSError:
        pass
    raise JudgeRunError("Cannot create judge output directory.")


class Recording:
    """Every intent is confirmed before calling; any recording error stops work."""

    def __init__(self, root):
        self.root = root
        self.events = []
        self.failed = False

    def write(self, name, content):
        temporary = None
        try:
            _check(
                not self.failed
                and name
                in ARTIFACTS | MCQ_PUBLICATION_ARTIFACTS | {"complete.json"}
            )
            target = self.root / name
            _check(not target.exists() and not target.is_symlink())
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as out:
                temporary = Path(out.name)
                out.write(content.encode())
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, target)
        except (Exception, KeyboardInterrupt):  # noqa: BLE001 - fail closed persistence boundary
            self.failed = True
            raise JudgeRunError("Judge artifact recording failed; no further calls allowed.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def emit(self, kind, **payload):
        try:
            _check(not self.failed)
            row = {"schema_version": 4, "judgment_id": self.root.name,
                   "seq": len(self.events) + 1, "time": datetime.now(UTC).isoformat(),
                   "type": kind, **payload}
            data = canonical(row) + "\n"
            path = self.root / "calls.jsonl"
            _check(not path.is_symlink())
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            self.events.append(json.loads(data))
        except (Exception, KeyboardInterrupt):  # noqa: BLE001 - fail closed persistence boundary
            self.failed = True
            raise JudgeRunError("Judge event recording failed; no further calls allowed.") from None


def _source(run_dir):
    root = Path(run_dir).expanduser().resolve()
    manifest_raw, trace_raw = _read(root, "manifest.json"), _read(root, "traces.jsonl")
    result = inspect_run(root)
    _check(result["trace_integrity"] == "complete"
           and result["run_status"] in {"completed", "empty"},
           "Judge requires a complete completed/empty generation trace.")
    _check(result["manifest_sha256"] == digest(manifest_raw)
           and result["trace_sha256"] == digest(trace_raw), "Source changed during inspection.")
    events = [json.loads(line) for line in trace_raw.splitlines()]
    materials = next(e["payload"]["chunks"] for e in events if e["event_type"] == "materials_selected")
    judge.validate_materials(materials)
    manifest = json.loads(manifest_raw)
    return root, result, {"schema_version": 4, "source": result, "chunks": materials,
                          "items": result["items"], "generation_model": manifest["config"].get("generation_model")}


def _phase(status, reason=None, raw_response=None):
    return {"status": status, "technical_status": "error" if status == "error" else "not_evaluated",
            "reason": reason, "model_review": None, "raw_response": raw_response,
            "verification": {"structure": "invalid" if reason == "schema_error" else "not_checked",
                             "evidence": "not_checked", "consistency": "not_checked",
                             "diagnostics": [{"code": reason, "path": "$"}] if reason else [],
                             "evidence_records": []},
            "human_review": {"status": "not_evaluated", "reasons": []}}


def _classify(phase, finish, item, chunks, *, anchor_origin="post_policy"):
    if finish["status"] != "response":
        return _phase("error", finish["error_kind"])
    response = finish["response"]
    content = response["content"]
    if response["finish_reason"] != "stop":
        return _phase("error", "refusal" if response["finish_reason"] == "refusal" else "truncated_response", content)
    try:
        value = judge.validate_review(phase, content, item, chunks, anchor_origin=anchor_origin)
    except judge.JudgeOutputError as exc:
        result = _phase("error", exc.kind, content)
        result["verification"]["diagnostics"] = exc.diagnostics
        return result
    return {"status": "evaluated", "technical_status": "completed",
            "reason": None, "raw_response": content, **value}


def _run_status(rows, *, execute, interrupted, max_items):
    if interrupted:
        return "interrupted"
    if not execute:
        return "prepared"
    selected = [row for row in rows if row["post_policy"] is not None][:max_items]
    return ("completed" if all(row[phase]["status"] == "evaluated"
                               for row in selected for phase in PHASES) else "incomplete")


def _transient(exc):
    # No dependence on exception strings, headers, or request objects.
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    from openai import APIConnectionError, APIStatusError
    return (isinstance(exc, APIConnectionError)
            or isinstance(exc, APIStatusError) and (exc.status_code == 429 or exc.status_code >= 500))


def _key(manifest, item_id, phase, request):
    return digest(canonical({"source": manifest["source_identity"],
                             "protocol": manifest["protocol_digest"],
                             "config": manifest["judge_config"],
                             "item_id": item_id, "phase": phase, "request": request}).encode())


def _request_size(request):
    return len(canonical(request["messages"]))


def _mcq_source(run_dir):
    root = Path(run_dir).expanduser().resolve()
    result = inspect_run(root)
    _check(
        result.get("generation_contract") == "mcq-v1"
        and result["trace_integrity"] == "complete"
        and result["run_status"] in {"completed", "empty"},
        "MCQ Judge requires a complete mcq-v1 generation trace.",
    )
    manifest_raw = _read(root, "manifest.json")
    _check(
        digest(manifest_raw) == result.get("manifest_sha256"),
        "Generation manifest changed after integrity inspection.",
    )
    trace_raw = _read(root, "traces.jsonl")
    _check(
        digest(trace_raw) == result.get("trace_sha256"),
        "Generation event log changed after integrity inspection.",
    )
    manifest = _json(manifest_raw)
    events = []
    for line in trace_raw.splitlines(keepends=True):
        _check(line.endswith(b"\n"), "Truncated generation trace event.")
        events.append(_json(line))
    chunks, seen = [], set()
    chunks_by_batch = {}
    balance = None
    for event in events:
        if event["event_type"] == "materials_selected":
            chunks_by_batch[event["payload"]["batch_index"]] = event["payload"]["chunks"]
            for chunk in event["payload"]["chunks"]:
                if chunk["id"] not in seen:
                    chunks.append(chunk)
                    seen.add(chunk["id"])
        elif event["event_type"] == "options_balanced":
            balance = event["payload"]
    judge.validate_materials(chunks)
    _check(balance is not None and len(balance["item_material_batches"]) == len(result["items"]))
    item_materials = [
        chunks_by_batch[batch] for batch in balance["item_material_batches"]
    ]
    return root, result, {
        "schema_version": 1,
        "generation_contract": "mcq-v1",
        "source": {
            "run_id": result["run_id"],
            "manifest_sha256": digest(manifest_raw),
            "trace_sha256": digest(trace_raw),
        },
        "course": manifest["task"]["course"],
        "chunks": chunks,
        "item_materials": item_materials,
        "items": result["items"],
        "generation_model": manifest["config"].get("generation_model"),
    }


def _mcq_report(metrics, rows):
    lines = [
        "# MCQ Judge report",
        "",
        f"- Status: **{metrics['run_status']}**",
        f"- Retained: {metrics['retained_count']}",
        f"- Accepted for judged publication: {metrics['accepted_count']}",
        f"- Pending human review: {metrics['human_review_pending_item_count']}",
        f"- Technical errors: {metrics['technical_error_phase_count']}",
        f"- Calls: {metrics['call_starts']}/{metrics['max_calls']}",
        "",
    ]
    for index, row in enumerate(rows, 1):
        lines.extend(
            [
                f"## Item {index}",
                "",
                f"- Status: `{row['status']}`",
                f"- Mandatory pass: `{row.get('mandatory_pass')}`",
                f"- Human review: `{row.get('human_review')}`",
                "",
            ]
        )
    return "\n".join(lines)


def _mcq_metrics(tasks, rows, events, *, mode, max_items, max_calls, status):
    selected_count = min(len(tasks["items"]), max_items or len(tasks["items"]))
    selected_rows = rows[:selected_count]
    return {
        "schema_version": 1,
        "mode": mode,
        "run_status": status,
        "retained_count": len(tasks["items"]),
        "accepted_count": sum(
            row.get("mandatory_pass") is True
            and row.get("human_review") == "not_required"
            for row in selected_rows
        ) if status == "completed" else 0,
        "selected_evaluated_phase_count": sum(
            row["status"] == "evaluated" for row in selected_rows
        ),
        "selected_phase_count": selected_count,
        "evaluated_phase_count": sum(row["status"] == "evaluated" for row in rows),
        "planned_phase_count": len(tasks["items"]),
        "model_all_positive_count": sum(
            row.get("mandatory_pass") is True
            and row.get("human_review") == "not_required"
            for row in selected_rows
        ),
        "human_review_pending_item_count": sum(
            row.get("human_review") == "pending" for row in selected_rows
        ),
        "technical_error_phase_count": sum(
            row["status"] == "error" for row in selected_rows
        ),
        "call_starts": sum(event.get("type") == "call_started" for event in events),
        "response_count": sum(
            event.get("type") == "call_finished" and event.get("status") == "response"
            for event in events
        ),
        "retry_count": sum(
            event.get("type") == "call_started" and event.get("attempt", 1) > 1
            for event in events
        ),
        "max_calls": max_calls,
    }


def run_mcq_judge(
    run_dir,
    *,
    execute=False,
    max_items=None,
    settings=None,
    client_factory=None,
):
    """Explicit, finite, one-phase MCQ Judge; negative items are never rewritten."""
    _check(max_items is None or _positive(max_items), "max-items must be positive.")
    if execute:
        try:
            settings = settings or judge.JudgeSettings()
        except (ValidationError, ValueError):
            raise JudgeRunError(
                "Invalid judge configuration; set JUDGE_MODEL, JUDGE_BASE_URL, "
                "JUDGE_API_KEY and valid limits."
            ) from None
    root, _source_result, tasks = _mcq_source(run_dir)
    target = _unique(root / "judgments")
    record = Recording(target)
    selected = tasks["items"][:max_items]
    ceiling = len(selected) * settings.max_attempts if execute else 0
    mode = "execute" if execute else "prepare"
    manifest = {
        "schema_version": 1,
        "judgment_id": target.name,
        "mode": mode,
        "created_at": datetime.now(UTC).isoformat(),
        "protocol_version": judge.MCQ_PROTOCOL_VERSION,
        "source_identity": tasks["source"],
        "judge_config": judge.safe_identity(settings) if execute else None,
        "max_calls": ceiling,
        "max_items": max_items,
        "generation_contract": "mcq-v1",
    }
    record.write("manifest.json", canonical(manifest) + "\n")
    record.write("tasks.json", canonical(tasks) + "\n")
    record.write("calls.jsonl", "")
    calls_used = 0
    client = None
    rows = []
    interrupted = False
    for index, item in enumerate(tasks["items"]):
        if index >= len(selected):
            rows.append({"item": item, "status": "not_evaluated", "reason": "max_items"})
            continue
        if not execute:
            rows.append({"item": item, "status": "not_evaluated", "reason": "prepare"})
            continue
        request = judge.build_mcq_request(
            item, tasks["item_materials"][index], settings
        )
        if _request_size(request) > settings.max_input_chars:
            rows.append({"item": item, "status": "error", "reason": "input_limit"})
            continue
        decision = None
        for attempt in range(1, settings.max_attempts + 1):
            _check(calls_used < ceiling, "MCQ Judge call ceiling exhausted.")
            if client is None:
                try:
                    client = (client_factory or judge.make_client)(settings)
                except Exception:  # noqa: BLE001 - never expose SDK configuration
                    raise JudgeRunError("Judge client initialization failed.") from None
            call_id = str(uuid4())
            record.emit(
                "call_started", call_id=call_id, item_index=index + 1,
                attempt=attempt, request=request,
            )
            calls_used += 1
            started = monotonic()
            retryable = False
            try:
                response = judge.invoke(client, deepcopy(request))
                finish = {
                    "status": "response", "response": response,
                    "error": None, "retryable": False,
                }
            except KeyboardInterrupt as exc:
                interrupted = True
                finish = {
                    "status": "interrupted", "response": None,
                    "error": safe_error(exc), "retryable": False,
                }
            except Exception as exc:  # noqa: BLE001 - safe classified boundary
                retryable = _transient(exc)
                finish = {
                    "status": "error", "response": None,
                    "error": safe_error(exc), "retryable": retryable,
                }
            record.emit(
                "call_finished", call_id=call_id, item_index=index + 1,
                attempt=attempt, elapsed_seconds=monotonic() - started, **finish,
            )
            if finish["status"] != "response":
                decision = {"item": item, "status": "error", "reason": finish["status"]}
                if not retryable or interrupted:
                    break
                continue
            response = finish["response"]
            if response["finish_reason"] != "stop":
                decision = {"item": item, "status": "error", "reason": "truncated_response"}
                break
            try:
                checked = judge.validate_mcq_review(response["content"], item)
            except judge.JudgeOutputError:
                decision = {"item": item, "status": "error", "reason": "schema_error"}
                continue
            decision = {"item": item, "status": "evaluated", "reason": None, **checked}
            break
        rows.append(decision or {"item": item, "status": "error", "reason": "call_ceiling"})
        if interrupted:
            break
    while len(rows) < len(tasks["items"]):
        rows.append({"item": tasks["items"][len(rows)], "status": "not_evaluated",
                     "reason": "interrupted"})
    selected_rows = rows[: len(selected)]
    complete = all(row["status"] == "evaluated" for row in selected_rows)
    status = "interrupted" if interrupted else "prepared" if not execute else (
        "completed" if complete else "incomplete"
    )
    accepted = [
        row["item"] for row in selected_rows
        if row.get("mandatory_pass") is True and row.get("human_review") == "not_required"
    ] if status == "completed" else []
    metrics = _mcq_metrics(
        tasks, rows, record.events, mode=mode, max_items=max_items,
        max_calls=ceiling, status=status,
    )
    publication = None
    if status == "completed":
        from overfit.models import GeneratedMCQExam, GeneratedMCQItem
        from overfit.query.generator import render_exam
        from overfit.query.mcq import balance_options, mix_items

        accepted_models = [GeneratedMCQItem.model_validate(item) for item in accepted]
        publication_seed = digest(
            f"{tasks['source']['run_id']}:{target.name}:judged".encode()
        )
        balanced = balance_options(accepted_models, seed_identity=publication_seed)
        final_items = mix_items(balanced.items, seed_identity=publication_seed)
        paper, answers = render_exam(
            tasks["course"],
            GeneratedMCQExam(items=final_items),
            answers_file="judged_answers.md",
            generated_on=manifest["created_at"][:10],
        )
        record.write("judged_mock_exam.md", paper)
        record.write("judged_answers.md", answers)
        accepted = [item.model_dump() for item in final_items]
        publication = {
            "paper": str(target / "judged_mock_exam.md"),
            "answers": str(target / "judged_answers.md"),
            "seed_identity": publication_seed,
        }
    record.write("judgments.jsonl", "".join(canonical(row) + "\n" for row in rows))
    record.write("review_queue.jsonl", "".join(
        canonical({"item_index": index + 1, "status": "pending"}) + "\n"
        for index, row in enumerate(rows) if row.get("human_review") == "pending"
    ))
    record.write("metrics.json", canonical(metrics) + "\n")
    record.write("report.md", _mcq_report(metrics, rows))
    artifact_names = ARTIFACTS | (MCQ_PUBLICATION_ARTIFACTS if publication else set())
    hashes = {name: digest(_read(target, name)) for name in sorted(artifact_names)}
    record.write("complete.json", canonical({
        "schema_version": 1, "judgment_id": target.name,
        "status": status, "artifacts": hashes,
    }) + "\n")
    return {
        "judgment_dir": str(target),
        "report_path": str(target / "report.md"),
        "status": status,
        "metrics": metrics,
        "accepted_items": accepted,
        "publication": publication,
        "exit_code": 130 if interrupted else 1 if status == "incomplete" else 0,
    }


def run_judge(run_dir, *, execute=False, max_calls=0, max_items=None, settings=None, client_factory=None):
    """Prepare by default. Execution always needs explicit config and call budget."""
    preview = inspect_run(Path(run_dir).expanduser().resolve())
    if preview.get("generation_contract") == "mcq-v1":
        return run_mcq_judge(
            run_dir,
            execute=execute,
            max_items=max_items,
            settings=settings,
            client_factory=client_factory,
        )
    _check(type(max_calls) is int and max_calls >= 0, "max-calls must be nonnegative.")
    _check(max_items is None or _positive(max_items), "max-items must be positive.")
    if execute:
        _check(_positive(max_calls), "--execute requires an explicit positive --max-calls budget.")
        try:
            settings = settings or judge.JudgeSettings()
        except (ValidationError, ValueError):
            raise JudgeRunError("Invalid judge configuration; set JUDGE_MODEL, JUDGE_BASE_URL, JUDGE_API_KEY and valid limits.") from None
    root, source, tasks = _source(run_dir)
    target = _unique(root / "judgments")
    record = Recording(target)
    mode = "execute" if execute else "prepare"
    manifest = {"schema_version": 4, "judgment_id": target.name, "mode": mode,
                "created_at": datetime.now(UTC).isoformat(),
                "protocol_version": judge.PROTOCOL_VERSION, "protocol_digest": judge.protocol_digest(),
                "source_identity": {"run_id": source["run_id"], "manifest_sha256": source["manifest_sha256"],
                                    "trace_sha256": source["trace_sha256"]},
                "judge_config": judge.safe_identity(settings) if execute else None,
                "max_calls": max_calls, "max_items": max_items,
                "calibration": "not_calibrated", "cache_policy": "no_reuse",
                "citation_policy": judge.POLICY_VERSION, "anchor_origin": "post_policy",
                "context_scope": "original_generation_context", "supplemental_review_context": None,
                "checker_identity": _checker_identity(),
                "same_model_as_generator": (settings.model == tasks["generation_model"] if execute else None)}
    record.write("manifest.json", canonical(manifest) + "\n")
    record.write("tasks.json", canonical(tasks) + "\n")
    record.write("calls.jsonl", "")
    calls_used, selected, client = 0, 0, None
    interrupted = False
    rows = []
    for original in tasks["items"]:
        row = deepcopy(original)
        row.pop("semantic_status", None)  # PR1-only marker is not a PR2 outcome.
        item = row["post_policy"]
        if item is not None:
            selected += 1
        for phase in PHASES:
            reason = ("product_dropped" if item is None else
                      "prepare" if not execute else
                      "interrupted" if interrupted else
                      "max_items" if max_items is not None and selected > max_items else None)
            request = None
            if reason is None:
                request = judge.build_request(phase, item, tasks["chunks"], settings)
                if _request_size(request) > settings.max_input_chars:
                    reason = "input_limit"
            decision = _phase("not_evaluated", reason)
            if reason is None:
                for attempt in range(1, settings.max_attempts + 1):
                    if calls_used >= max_calls:
                        if decision["status"] != "error":
                            decision = _phase("not_evaluated", "call_budget")
                        break
                    # Client construction is local; no network request before intent.
                    if client is None:
                        try:
                            client = (client_factory or judge.make_client)(settings)
                        except Exception:  # noqa: BLE001 - never expose SDK configuration
                            raise JudgeRunError("Judge client initialization failed.") from None
                    call_id = str(uuid4())
                    record.emit("call_started", call_id=call_id, item_id=row["item_id"], phase=phase,
                                attempt=attempt, request=request, cache_key=_key(manifest, row["item_id"], phase, request))
                    calls_used += 1
                    started = monotonic()
                    retry = False
                    try:
                        response = judge.invoke(client, deepcopy(request))
                        finish = {"status": "response", "response": response, "error_kind": None, "error": None}
                    except KeyboardInterrupt as exc:
                        interrupted = True
                        finish = {"status": "interrupted", "response": None, "error_kind": "interrupted", "error": safe_error(exc)}
                    except Exception as exc:  # noqa: BLE001 - capture provider failures without secrets
                        retry = _transient(exc)
                        finish = {"status": "error", "response": None,
                                  "error_kind": "transport_error" if retry else "provider_error", "error": safe_error(exc)}
                    record.emit("call_finished", call_id=call_id, item_id=row["item_id"], phase=phase,
                                attempt=attempt, elapsed_seconds=monotonic() - started, **finish)
                    decision = _classify(phase, finish, item, tasks["chunks"])
                    retry = retry or decision["reason"] == "schema_error"
                    if not retry or interrupted:
                        break
            row[phase] = decision
            record.emit("task_finished", item_id=row["item_id"], phase=phase, result=decision)
        rows.append(row)
    metrics = summarize_judgments(source, rows, record.events, mode, max_items=max_items)
    status = _run_status(rows, execute=execute, interrupted=interrupted, max_items=max_items)
    metrics.update(judgment_id=target.name, run_status=status, record_integrity="complete",
                   protocol_version=judge.PROTOCOL_VERSION, protocol_digest=manifest["protocol_digest"],
                   same_model_as_generator=manifest["same_model_as_generator"],
                   source_identity=manifest["source_identity"], checker_identity=_checker_identity())
    record.write("judgments.jsonl", "".join(canonical(r) + "\n" for r in rows))
    record.write("review_queue.jsonl", "".join(canonical(r) + "\n" for r in build_review_queue(rows)))
    record.write("metrics.json", canonical(metrics) + "\n")
    record.write("report.md", render_judge_report(metrics, rows))
    hashes = {name: digest(_read(target, name)) for name in sorted(ARTIFACTS)}
    record.write("complete.json", canonical({"schema_version": 4, "judgment_id": target.name,
                                             "status": status, "artifacts": hashes}) + "\n")
    return {"judgment_dir": str(target), "report_path": str(target / "report.md"),
            "status": status, "metrics": metrics, "exit_code": 130 if interrupted else
            1 if status == "incomplete" else 0}


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _check(key not in result, "Duplicate JSON key in judge recording.")
            result[key] = value
        return result
    def invalid_constant(_):
        raise JudgeRunError("Non-finite JSON number.")
    return json.loads(data, object_pairs_hook=unique, parse_constant=invalid_constant)


def _reconstruct(root):
    """Require an intact snapshot, then rederive task results from visible content."""
    complete = _json(_read(root, "complete.json"))
    _check(type(complete["schema_version"]) is int and complete["schema_version"] == 4,
           "Unsupported judge recording version; use its recorded implementation.")
    _check(set(complete["artifacts"]) == ARTIFACTS)
    raw = {name: _read(root, name) for name in ARTIFACTS}
    _check(all(digest(data) == complete["artifacts"][name] for name, data in raw.items()),
           "Judge artifact digest mismatch.")
    manifest, tasks = _json(raw["manifest.json"]), _json(raw["tasks.json"])
    _check(all(type(v["schema_version"]) is int and v["schema_version"] == 4 for v in (manifest, tasks)))
    _check(manifest["judgment_id"] == complete["judgment_id"] == root.name)
    _check(manifest["protocol_version"] == judge.PROTOCOL_VERSION
           and manifest["protocol_digest"] == judge.protocol_digest()
           and manifest["checker_identity"] == _checker_identity(),
           "Unsupported judge protocol implementation; use its recorded version.")
    _check(manifest.get("citation_policy") == judge.POLICY_VERSION
           and manifest.get("anchor_origin") == "post_policy"
           and manifest.get("context_scope") == "original_generation_context"
           and manifest.get("supplemental_review_context") is None,
           "Invalid judge location policy or context scope.")
    _check(manifest["mode"] in {"prepare", "execute"})
    _, source, actual_tasks = _source(root.parent.parent)
    _check(tasks == actual_tasks, "Source trace no longer matches the judge input snapshot.")
    _check(manifest["source_identity"] == {key: source[key] for key in
                                          ("run_id", "manifest_sha256", "trace_sha256")})
    execute = manifest["mode"] == "execute"
    config = manifest["judge_config"]
    if execute:
        _check(isinstance(config, dict) and set(config) == {"base_url", "model", "timeout", "max_output_tokens",
                                                           "temperature", "max_attempts", "max_input_chars"})
        _check(all(_positive(config[k]) for k in ("max_output_tokens", "max_attempts", "max_input_chars")))
        _check(isinstance(config["model"], str) and bool(config["model"].strip()))
        _check(type(config["timeout"]) in (int, float) and math.isfinite(config["timeout"]) and config["timeout"] > 0)
        _check(type(config["temperature"]) in (int, float) and 0 <= config["temperature"] <= 2)
        settings = SimpleNamespace(**config)  # No settings or environment access in replay.
        _check(_positive(manifest["max_calls"]))
    else:
        _check(config is None)
        settings = None
    _check(type(manifest["max_calls"]) is int and manifest["max_calls"] >= 0)
    _check(manifest["max_items"] is None or _positive(manifest["max_items"]))
    rows = deepcopy(tasks["items"])
    for row in rows:
        row.pop("semantic_status", None)
    schedule = [(row, phase) for row in rows for phase in PHASES]
    selected = {row["item_id"]: i for i, row in enumerate(
        (r for r in rows if r["post_policy"] is not None), 1)}
    events, active, finishes, ids = [], None, [], set()
    position, call_count, attempt, interrupted = 0, 0, 0, False
    for line in raw["calls.jsonl"].splitlines(keepends=True):
        _check(line.endswith(b"\n"), "Truncated judge event log.")
        event = _json(line)
        _check(type(event["schema_version"]) is int and event["schema_version"] == 4)
        _check(event["judgment_id"] == root.name and type(event["seq"]) is int
               and event["seq"] == len(events) + 1)
        stamp = datetime.fromisoformat(event["time"])
        _check(stamp.utcoffset() is not None and stamp.utcoffset().total_seconds() == 0)
        _check(position < len(schedule), "Events after the final judge task.")
        row, phase = schedule[position]
        _check(event["item_id"] == row["item_id"] and event["phase"] == phase)
        item = row["post_policy"]
        skip = ("product_dropped" if item is None else "prepare" if not execute else
                "interrupted" if interrupted else "max_items" if manifest["max_items"] is not None
                and selected[row["item_id"]] > manifest["max_items"] else None)
        request = judge.build_request(phase, item, tasks["chunks"], settings) if skip is None else None
        if request is not None and _request_size(request) > settings.max_input_chars:
            skip = "input_limit"
        kind = event["type"]
        if kind == "call_started":
            _check(skip is None and active is None and call_count < manifest["max_calls"])
            _check(isinstance(event["call_id"], str) and event["call_id"] and event["call_id"] not in ids)
            _check(type(event["attempt"]) is int and event["attempt"] == attempt + 1
                   and event["attempt"] <= settings.max_attempts)
            if finishes:
                previous = _classify(phase, finishes[-1], item, tasks["chunks"])
                _check(previous["reason"] in {"schema_error", "transport_error"}, "Unexpected judge retry.")
            _check(event["request"] == request
                   and event["cache_key"] == _key(manifest, row["item_id"], phase, request),
                   "Judge request snapshot mismatch.")
            ids.add(event["call_id"])
            active, attempt = event, event["attempt"]
            call_count += 1
        elif kind == "call_finished":
            _check(active is not None and event["call_id"] == active["call_id"]
                   and type(event["attempt"]) is int and event["attempt"] == attempt)
            _check(event["status"] in {"response", "error", "interrupted"})
            _check(type(event["elapsed_seconds"]) in (int, float)
                   and math.isfinite(event["elapsed_seconds"]) and event["elapsed_seconds"] >= 0)
            if event["status"] == "response":
                response = event["response"]
                _check(isinstance(response, dict) and isinstance(response["content"], str))
                _check(response["finish_reason"] is None or isinstance(response["finish_reason"], str))
                _check(response["usage"] is None or isinstance(response["usage"], dict)
                       and all(k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                               and type(v) is int and v >= 0 for k, v in response["usage"].items()))
                _check(event["error_kind"] is None and event["error"] is None)
            else:
                _check(event["response"] is None and isinstance(event["error"], dict))
                _check(event["error_kind"] in {"transport_error", "provider_error", "interrupted"})
                if event["status"] == "interrupted":
                    _check(event["error_kind"] == "interrupted")
                    interrupted = True
            finishes.append(event)
            active = None
        elif kind == "task_finished":
            _check(active is None)
            if finishes:
                expected = _classify(phase, finishes[-1], item, tasks["chunks"])
                _check(expected["reason"] not in {"schema_error", "transport_error"}
                       or attempt >= settings.max_attempts or call_count >= manifest["max_calls"]
                       or interrupted, "Retryable task stopped before its fixed retry limit.")
            else:
                if skip is None:
                    _check(call_count >= manifest["max_calls"], "Unevaluated task without a limiting condition.")
                    skip = "call_budget"
                expected = _phase("not_evaluated", skip)
            _check(event["result"] == expected, "Stored task result disagrees with the raw judge response.")
            row[phase] = expected
            position += 1
            finishes, attempt = [], 0
        else:
            raise JudgeRunError("Unknown judge event type.")
        events.append(event)
    _check(position == len(schedule) and active is None, "Incomplete judge task log.")
    status = _run_status(rows, execute=execute, interrupted=interrupted, max_items=manifest["max_items"])
    _check(complete["status"] == status, "Judge terminal status conflicts with observed tasks.")
    _check(manifest["same_model_as_generator"] is (
        config["model"] == tasks["generation_model"] if execute else None))
    metrics = summarize_judgments(source, rows, events, manifest["mode"], max_items=manifest["max_items"])
    metrics.update(judgment_id=root.name, run_status=status, record_integrity="complete",
                   protocol_version=judge.PROTOCOL_VERSION, protocol_digest=manifest["protocol_digest"],
                   same_model_as_generator=manifest["same_model_as_generator"],
                   source_identity=manifest["source_identity"], checker_identity=_checker_identity())
    metrics["input_artifact_digests"] = complete["artifacts"]
    return metrics, rows


def replay_judgment(judgment_dir):
    """Offline verification; never trusts persisted metrics or normalized reviews."""
    root = Path(judgment_dir).expanduser().resolve()
    try:
        preview = _json(_read(root, "manifest.json"))
    except (ValueError, TypeError, JudgeRunError):
        raise JudgeRunError("Invalid or unsupported judge recording.") from None
    if preview.get("protocol_version") == judge.MCQ_PROTOCOL_VERSION:
        return _replay_mcq_judgment(root)
    try:
        metrics, rows = _reconstruct(root)
    except JudgeRunError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        raise JudgeRunError("Invalid or unsupported judge recording.") from None
    target = _unique(root / "replays")
    record = Recording(target)
    record.write("judgments.jsonl", "".join(canonical(r) + "\n" for r in rows))
    record.write("review_queue.jsonl", "".join(canonical(r) + "\n" for r in build_review_queue(rows)))
    metrics["replay_id"] = target.name
    metrics["replayed_at"] = datetime.now(UTC).isoformat()
    record.write("metrics.json", canonical(metrics) + "\n")
    record.write("report.md", render_judge_report(metrics, rows))
    return {"replay_dir": str(target), "report_path": str(target / "report.md"),
            "status": metrics["run_status"], "metrics": metrics,
            "exit_code": 130 if metrics["run_status"] == "interrupted" else
            1 if metrics["run_status"] == "incomplete" else 0}


def _replay_mcq_judgment(root: Path):
    """Verify an MCQ Judge recording and its raw-response decisions offline."""
    try:
        complete = _json(_read(root, "complete.json"))
        _check(
            set(complete) == {"schema_version", "judgment_id", "status", "artifacts"}
            and type(complete["schema_version"]) is int
            and complete["schema_version"] == 1
            and complete["judgment_id"] == root.name,
            "Invalid MCQ Judge completion envelope.",
        )
        expected_artifacts = ARTIFACTS | (
            MCQ_PUBLICATION_ARTIFACTS
            if complete.get("status") == "completed"
            else set()
        )
        _check(set(complete.get("artifacts", {})) == expected_artifacts)
        raw = {name: _read(root, name) for name in expected_artifacts}
        _check(all(digest(raw[name]) == value for name, value in complete["artifacts"].items()))
        manifest = _json(raw["manifest.json"])
        tasks = _json(raw["tasks.json"])
        stored_rows = [_json(line) for line in raw["judgments.jsonl"].splitlines()]
        events = [_json(line) for line in raw["calls.jsonl"].splitlines()]
        task_fields = {
            "schema_version", "generation_contract", "source", "course", "chunks",
            "item_materials", "items", "generation_model",
        }
        _check(
            set(tasks) == task_fields
            and type(tasks["schema_version"]) is int
            and tasks["schema_version"] == 1,
            "Invalid MCQ Judge task envelope.",
        )
        manifest_fields = {
            "schema_version", "judgment_id", "mode", "created_at",
            "protocol_version", "source_identity", "judge_config", "max_calls",
            "max_items", "generation_contract",
        }
        _check(set(manifest) == manifest_fields, "Invalid MCQ Judge manifest envelope.")
        _check(
            type(manifest["schema_version"]) is int
            and manifest["schema_version"] == 1
            and manifest["judgment_id"] == complete["judgment_id"] == root.name,
            "MCQ Judge manifest identity mismatch.",
        )
        created_at = datetime.fromisoformat(manifest["created_at"])
        _check(
            created_at.utcoffset() is not None
            and created_at.utcoffset().total_seconds() == 0,
            "MCQ Judge created_at must be UTC.",
        )
        _check(
            manifest["source_identity"] == tasks.get("source"),
            "MCQ Judge source identity does not match its task snapshot.",
        )
        _check(manifest["generation_contract"] == tasks["generation_contract"] == "mcq-v1")
        _check(manifest["protocol_version"] == judge.MCQ_PROTOCOL_VERSION)
        _check(len(stored_rows) == len(tasks["items"]))
        source_root = root.parent.parent
        _, _, actual = _mcq_source(source_root)
        _check(
            _same_json(actual, tasks),
            "Source trace no longer matches MCQ Judge input.",
        )
        mode = manifest["mode"]
        _check(mode in {"prepare", "execute"})
        max_items = manifest["max_items"]
        _check(max_items is None or _positive(max_items), "Invalid MCQ Judge max_items.")
        selected_count = min(len(tasks["items"]), max_items or len(tasks["items"]))
        if mode == "execute":
            config = manifest.get("judge_config")
            config_keys = {
                "base_url", "model", "timeout", "max_output_tokens", "temperature",
                "max_attempts", "max_input_chars",
            }
            _check(
                isinstance(config, dict)
                and set(config) == config_keys
                and isinstance(config["base_url"], str)
                and isinstance(config["model"], str)
                and type(config["timeout"]) is float
                and type(config["temperature"]) is float
                and all(_positive(config[key]) for key in (
                    "max_output_tokens", "max_attempts", "max_input_chars"
                )),
                "Invalid MCQ Judge configuration shape.",
            )
            settings = judge.JudgeSettings(
                _env_file=None,
                api_key="offline-replay-placeholder",
                **config,
            )
            _check(
                judge.safe_identity(settings) == config,
                "MCQ Judge configuration is not a canonical safe identity.",
            )
        else:
            _check(manifest.get("judge_config") is None,
                   "Prepare-only MCQ Judge recording has configuration.")
            settings = None
        expected_ceiling = selected_count * settings.max_attempts if mode == "execute" else 0
        _check(
            type(manifest.get("max_calls")) is int
            and manifest["max_calls"] == expected_ceiling,
            "MCQ Judge call ceiling is not derived from the selected item count.",
        )
        active = None
        attempts = {}
        finishes_by_item = {index: [] for index in range(1, selected_count + 1)}
        call_ids = set()
        last_started_item = 0
        for position, event in enumerate(events, 1):
            common_fields = {
                "schema_version", "judgment_id", "seq", "time", "type", "call_id",
                "item_index", "attempt",
            }
            event_type = event.get("type")
            expected_fields = (
                common_fields | {"request"}
                if event_type == "call_started"
                else common_fields | {
                    "elapsed_seconds", "status", "response", "error", "retryable"
                }
                if event_type == "call_finished"
                else set()
            )
            _check(
                bool(expected_fields)
                and set(event) == expected_fields
                and type(event["schema_version"]) is int
                and event["schema_version"] == 4
                and isinstance(event["judgment_id"], str)
                and event["judgment_id"] == root.name
                and type(event["seq"]) is int
                and event["seq"] == position
                and isinstance(event["time"], str)
                and isinstance(event["call_id"], str)
                and bool(event["call_id"])
                and type(event["item_index"]) is int
                and type(event["attempt"]) is int,
                "Invalid MCQ Judge call event envelope.",
            )
            stamp = datetime.fromisoformat(event["time"])
            _check(stamp.utcoffset() is not None and stamp.utcoffset().total_seconds() == 0)
            item_index = event["item_index"]
            _check(1 <= item_index <= selected_count)
            if event["type"] == "call_started":
                _check(isinstance(event["request"], dict), "Invalid MCQ Judge request event.")
                _check(active is None and event["call_id"] not in call_ids)
                _check(item_index >= last_started_item, "MCQ Judge item order regressed.")
                expected_attempt = attempts.get(item_index, 0) + 1
                _check(event["attempt"] == expected_attempt <= settings.max_attempts)
                if expected_attempt > 1:
                    previous = finishes_by_item[item_index][-1]
                    allowed_retry = previous["status"] == "error" and previous.get(
                        "retryable"
                    ) is True
                    if previous["status"] == "response" and previous["response"].get(
                        "finish_reason"
                    ) == "stop":
                        try:
                            judge.validate_mcq_review(
                                previous["response"]["content"],
                                tasks["items"][item_index - 1],
                            )
                        except judge.JudgeOutputError:
                            allowed_retry = True
                    _check(allowed_retry, "MCQ Judge retry was not admissible.")
                request = judge.build_mcq_request(
                    tasks["items"][item_index - 1],
                    tasks["item_materials"][item_index - 1],
                    settings,
                )
                _check(_request_size(request) <= settings.max_input_chars
                       and _same_json(event["request"], request))
                attempts[item_index] = expected_attempt
                last_started_item = item_index
                call_ids.add(event["call_id"])
                active = event
            elif event["type"] == "call_finished":
                _check(active is not None and event["call_id"] == active["call_id"]
                       and event["item_index"] == active["item_index"]
                       and event["attempt"] == active["attempt"])
                _check(event["status"] in {"response", "error", "interrupted"})
                _check(type(event.get("retryable")) is bool)
                _check((event["status"] == "error") or event["retryable"] is False)
                _check(type(event["elapsed_seconds"]) is float
                       and math.isfinite(event["elapsed_seconds"])
                       and event["elapsed_seconds"] >= 0)
                if event["status"] == "response":
                    response = event["response"]
                    _check(
                        isinstance(response, dict)
                        and set(response) == {"content", "usage", "finish_reason", "model"}
                        and isinstance(response["content"], str)
                        and (
                            response["finish_reason"] is None
                            or isinstance(response["finish_reason"], str)
                        )
                        and (response["model"] is None or isinstance(response["model"], str))
                        and event["error"] is None
                        and event["retryable"] is False,
                        "Invalid MCQ Judge response payload.",
                    )
                    usage = response["usage"]
                    _check(
                        usage is None
                        or (
                            isinstance(usage, dict)
                            and bool(usage)
                            and set(usage) <= {
                                "prompt_tokens", "completion_tokens", "total_tokens"
                            }
                            and all(type(value) is int and value >= 0
                                    for value in usage.values())
                        ),
                        "Invalid MCQ Judge usage payload.",
                    )
                else:
                    safe_messages = {
                        "interrupted": "Operation interrupted.",
                        "trace_write_error": "Trace recording failed.",
                        "timeout": "Operation timed out.",
                        "io_error": "An I/O operation failed.",
                        "validation_error": "Data validation failed.",
                        "operation_error": "Operation failed.",
                    }
                    error = event["error"]
                    _check(
                        event["response"] is None
                        and isinstance(error, dict)
                        and set(error) == {"type", "message"}
                        and error.get("type") in safe_messages
                        and error.get("message") == safe_messages[error["type"]]
                        and ((event["status"] == "interrupted")
                             is (error["type"] == "interrupted")),
                        "Invalid MCQ Judge safe error payload.",
                    )
                finishes_by_item[item_index].append(event)
                active = None
            else:
                _check(False, "Unknown MCQ Judge event.")
        _check(active is None and len(call_ids) <= manifest["max_calls"])

        interrupted_seen = False
        for index in range(1, selected_count + 1):
            request = judge.build_mcq_request(
                tasks["items"][index - 1], tasks["item_materials"][index - 1], settings
            ) if mode == "execute" else None
            item_finishes = finishes_by_item[index]
            if mode == "prepare" or _request_size(request) > settings.max_input_chars:
                _check(not item_finishes, "MCQ Judge called an ineligible item.")
                continue
            if interrupted_seen:
                _check(not item_finishes, "MCQ Judge continued after interruption.")
                continue
            _check(item_finishes, "MCQ Judge omitted an eligible item.")
            for offset, finish in enumerate(item_finishes, 1):
                retry_required = finish["status"] == "error" and finish["retryable"] is True
                if finish["status"] == "response" and finish["response"].get("finish_reason") == "stop":
                    try:
                        judge.validate_mcq_review(
                            finish["response"]["content"], tasks["items"][index - 1]
                        )
                    except judge.JudgeOutputError:
                        retry_required = True
                if offset < len(item_finishes):
                    _check(retry_required, "MCQ Judge retried a terminal result.")
                elif retry_required and offset < settings.max_attempts:
                    _check(False, "MCQ Judge omitted a required retry.")
                if finish["status"] == "interrupted":
                    interrupted_seen = True
                    _check(offset == len(item_finishes), "MCQ Judge continued after interruption.")

        reconstructed = []
        for index, item in enumerate(tasks["items"], 1):
            if index > selected_count:
                reconstructed.append({"item": item, "status": "not_evaluated", "reason": "max_items"})
                continue
            if mode == "prepare":
                reconstructed.append({"item": item, "status": "not_evaluated", "reason": "prepare"})
                continue
            request = judge.build_mcq_request(item, tasks["item_materials"][index - 1], settings)
            item_finishes = finishes_by_item[index]
            if _request_size(request) > settings.max_input_chars:
                decision = {"item": item, "status": "error", "reason": "input_limit"}
            elif not item_finishes:
                decision = {"item": item, "status": "not_evaluated", "reason": "interrupted"}
            else:
                finish = item_finishes[-1]
                if finish["status"] != "response":
                    decision = {"item": item, "status": "error", "reason": finish["status"]}
                elif finish["response"]["finish_reason"] != "stop":
                    decision = {"item": item, "status": "error", "reason": "truncated_response"}
                else:
                    try:
                        checked = judge.validate_mcq_review(finish["response"]["content"], item)
                    except judge.JudgeOutputError:
                        decision = {"item": item, "status": "error", "reason": "schema_error"}
                    else:
                        decision = {"item": item, "status": "evaluated", "reason": None, **checked}
            reconstructed.append(decision)
        interrupted = any(event.get("status") == "interrupted" for event in events)
        status = "prepared" if mode == "prepare" else "interrupted" if interrupted else (
            "completed" if all(row["status"] == "evaluated"
                               for row in reconstructed[:selected_count]) else "incomplete"
        )
        _check(_same_json(stored_rows, reconstructed) and complete["status"] == status,
               "MCQ Judge rows/status differ from raw calls.")
        metrics = _json(raw["metrics.json"])
        recomputed_metrics = _mcq_metrics(
            tasks, reconstructed, events, mode=mode, max_items=max_items,
            max_calls=manifest["max_calls"], status=status,
        )
        _check(_same_json(metrics, recomputed_metrics),
               "MCQ Judge metrics differ from raw calls.")
        _check(
            raw["report.md"].decode() == _mcq_report(metrics, stored_rows),
            "MCQ Judge report differs from reconstructed results.",
        )
        expected_queue = "".join(
            canonical({"item_index": index + 1, "status": "pending"}) + "\n"
            for index, row in enumerate(reconstructed)
            if row.get("human_review") == "pending"
        ).encode()
        _check(raw["review_queue.jsonl"] == expected_queue,
               "MCQ Judge review queue differs from raw calls.")
        if complete["status"] == "completed":
            from overfit.models import GeneratedMCQExam, GeneratedMCQItem
            from overfit.query.generator import render_exam
            from overfit.query.mcq import balance_options, mix_items

            accepted = [
                row["item"] for row in stored_rows
                if row.get("mandatory_pass") is True
                and row.get("human_review") == "not_required"
            ]
            seed = digest(
                f"{tasks['source']['run_id']}:{root.name}:judged".encode()
            )
            balanced = balance_options(
                [GeneratedMCQItem.model_validate(item) for item in accepted],
                seed_identity=seed,
            )
            final_items = mix_items(balanced.items, seed_identity=seed)
            paper, answers = render_exam(
                tasks["course"], GeneratedMCQExam(items=final_items),
                answers_file="judged_answers.md",
                generated_on=manifest["created_at"][:10],
            )
            _check(raw["judged_mock_exam.md"].decode() == paper
                   and raw["judged_answers.md"].decode() == answers,
                   "Judged publication is not derived from accepted MCQ items.")
            _check(metrics["accepted_count"] == len(accepted))
    except JudgeRunError:
        raise
    except (ValueError, TypeError, KeyError, IndexError):
        raise JudgeRunError("Invalid or unsupported MCQ Judge recording.") from None
    target = _unique(root / "replays")
    record = Recording(target)
    replay_metrics = deepcopy(metrics)
    replay_metrics["replay_id"] = target.name
    replay_metrics["replayed_at"] = datetime.now(UTC).isoformat()
    record.write("judgments.jsonl", "".join(canonical(row) + "\n" for row in stored_rows))
    record.write("review_queue.jsonl", raw["review_queue.jsonl"].decode())
    record.write("metrics.json", canonical(replay_metrics) + "\n")
    record.write("report.md", _mcq_report(replay_metrics, stored_rows))
    status = metrics["run_status"]
    return {
        "replay_dir": str(target),
        "report_path": str(target / "report.md"),
        "status": status,
        "metrics": replay_metrics,
        "exit_code": 130 if status == "interrupted" else 1 if status == "incomplete" else 0,
    }
