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


class JudgeRunError(OverfitError):
    """An explicit, safe error; never embeds provider or configuration secrets."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _check(value, message="Invalid judge recording."):
    if not value:
        raise JudgeRunError(message)


def _checker_identity():
    base = Path(__file__).parent
    return {name: digest((base / name).read_bytes())
            for name in ("judge.py", "runner.py", "metrics.py")}


def _positive(value):
    return type(value) is int and value > 0


def _read(root, name):
    _check(name in ARTIFACTS | {"complete.json", "traces.jsonl"}, "Unsupported artifact name.")
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
            _check(not self.failed and name in ARTIFACTS | {"complete.json"})
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
            row = {"schema_version": 3, "judgment_id": self.root.name,
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
    manifest = json.loads(manifest_raw)
    return root, result, {"schema_version": 3, "source": result, "chunks": materials,
                          "items": result["items"], "generation_model": manifest["config"].get("generation_model")}


def _phase(status, reason=None, raw_response=None):
    return {"status": status, "technical_status": "error" if status == "error" else "not_evaluated",
            "reason": reason, "model_review": None, "raw_response": raw_response,
            "verification": {"structure": "invalid" if reason == "schema_error" else "not_checked",
                             "evidence": "not_checked", "consistency": "not_checked",
                             "diagnostics": [{"code": reason, "path": "$"}] if reason else [],
                             "evidence_records": []},
            "human_review": {"status": "not_evaluated", "reasons": []}}


def _classify(phase, finish, item, chunks):
    if finish["status"] != "response":
        return _phase("error", finish["error_kind"])
    response = finish["response"]
    content = response["content"]
    if response["finish_reason"] != "stop":
        return _phase("error", "refusal" if response["finish_reason"] == "refusal" else "truncated_response", content)
    try:
        value = judge.validate_review(phase, content, item, chunks)
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


def run_judge(run_dir, *, execute=False, max_calls=0, max_items=None, settings=None, client_factory=None):
    """Prepare by default. Execution always needs explicit config and call budget."""
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
    manifest = {"schema_version": 3, "judgment_id": target.name, "mode": mode,
                "created_at": datetime.now(UTC).isoformat(),
                "protocol_version": judge.PROTOCOL_VERSION, "protocol_digest": judge.protocol_digest(),
                "source_identity": {"run_id": source["run_id"], "manifest_sha256": source["manifest_sha256"],
                                    "trace_sha256": source["trace_sha256"]},
                "judge_config": judge.safe_identity(settings) if execute else None,
                "max_calls": max_calls, "max_items": max_items,
                "calibration": "not_calibrated", "cache_policy": "no_reuse",
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
    record.write("complete.json", canonical({"schema_version": 3, "judgment_id": target.name,
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
    _check(type(complete["schema_version"]) is int and complete["schema_version"] == 3,
           "Unsupported judge recording version; use its recorded implementation.")
    _check(set(complete["artifacts"]) == ARTIFACTS)
    raw = {name: _read(root, name) for name in ARTIFACTS}
    _check(all(digest(data) == complete["artifacts"][name] for name, data in raw.items()),
           "Judge artifact digest mismatch.")
    manifest, tasks = _json(raw["manifest.json"]), _json(raw["tasks.json"])
    _check(all(type(v["schema_version"]) is int and v["schema_version"] == 3 for v in (manifest, tasks)))
    _check(manifest["judgment_id"] == complete["judgment_id"] == root.name)
    _check(manifest["protocol_version"] == judge.PROTOCOL_VERSION
           and manifest["protocol_digest"] == judge.protocol_digest()
           and manifest["checker_identity"] == _checker_identity(),
           "Unsupported judge protocol implementation; use its recorded version.")
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
        _check(type(event["schema_version"]) is int and event["schema_version"] == 3)
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
