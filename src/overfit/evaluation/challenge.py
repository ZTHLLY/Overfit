"""Opt-in, standalone judge challenge runner; never impersonates a generation trace."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from overfit.errors import OverfitError
from overfit.evaluation import judge, runner
from overfit.evaluation.citations import check_citation
from overfit.evaluation.metrics import _block, _diagnostics, _evidence, _usage
from overfit.evaluation.trace import safe_error

PROTOCOL_VERSION = "challenge-v1"
RECORD_KIND = "judge_challenge"
ARTIFACTS = {
    "inputs.json",
    "annotations.json",
    "manifest.json",
    "calls.jsonl",
    "judgments.jsonl",
    "metrics.json",
    "review_queue.jsonl",
    "report.md",
}
PHASES = ("answerability", "support")
DIMENSIONS = {
    "answerability": ("answerable", "not_answerable", "unknown"),
    "material_support": ("supported", "partially_supported", "unsupported", "unknown"),
    "citation_support": ("supported", "partially_supported", "unsupported", "unknown"),
}


class ChallengeError(OverfitError):
    """Only fixed, safe messages cross the CLI boundary."""


def _require(
    condition,
    message="Invalid challenge suite; check schemas, cases and paired changes.",
):
    if not condition:
        raise ChallengeError(message)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    @field_validator("*", mode="after")
    @classmethod
    def nonblank(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("Text cannot be blank")
        return value


class _Provenance(_Strict):
    source_tasks_path: str
    source_tasks_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_run_id: str
    source_judgment_id: str
    source_tasks_schema_version: int = Field(gt=0)


class _Chunk(_Strict):
    id: str
    source: str
    page: int = Field(gt=0)
    page_end: int | None
    section: str | None
    text: str


class _Input(_Strict):
    question: str
    answer: str
    source: str
    page: int = Field(gt=0)
    topic: str


class _Case(_Strict):
    case_id: str
    input: _Input


class _Inputs(_Strict):
    schema_id: Literal["overfit.judge_challenge_inputs.v1"] = Field(alias="schema")
    suite_id: str
    provenance: _Provenance
    chunks: list[_Chunk] = Field(min_length=1)
    cases: list[_Case] = Field(min_length=1)


class _Change(_Strict):
    field: Literal["question", "answer", "source", "page", "topic"]
    before: str | int
    after: str | int


class _Target(_Strict):
    primary_dimensions: list[
        Literal["answerability", "material_support", "citation_support"]
    ]
    goal: str
    label_policy: str


class _Annotation(_Strict):
    case_id: str
    case_type: Literal["control", "mutant"]
    control_case_id: str
    source_item_id: str
    changes: list[_Change]
    design_reason: str
    qualitative_target: _Target


class _Annotations(_Strict):
    schema_id: Literal["overfit.judge_challenge_annotations.v1"] = Field(alias="schema")
    suite_id: str
    annotation_status: Literal["human_designed_not_calibrated_not_model_results"]
    model_input_policy: str
    cases: list[_Annotation] = Field(min_length=1)


def _parse(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result)
            result[key] = value
        return result

    def invalid(_):
        raise ChallengeError("Non-finite numbers are not allowed in challenge inputs.")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def load_suite(suite_dir):
    """Validate the full local snapshot without settings, source-provenance reads or services."""
    root = Path(suite_dir).expanduser().absolute()
    try:
        _require(not root.is_symlink(), "Challenge suite symlinks are not supported.")
        snapshots = {}
        for name in ("inputs.json", "annotations.json"):
            path = root / name
            _require(
                not path.is_symlink(), "Challenge input symlinks are not supported."
            )
            snapshots[name] = path.read_bytes()
        inputs, annotations = (
            _parse(snapshots[name]) for name in ("inputs.json", "annotations.json")
        )
        _Inputs.model_validate(inputs)
        _Annotations.model_validate(annotations)
        _require(inputs["suite_id"] == annotations["suite_id"])
        chunks = inputs["chunks"]
        _require(len({c["id"] for c in chunks}) == len(chunks))
        check_citation({}, chunks)  # Checks each page range, never repairs materials.
        cases = {c["case_id"]: c["input"] for c in inputs["cases"]}
        notes = {a["case_id"]: a for a in annotations["cases"]}
        _require(
            len(cases) == len(inputs["cases"])
            and len(notes) == len(annotations["cases"])
        )
        _require(set(cases) == set(notes))
        for case_id, item in cases.items():
            _require(check_citation(item, chunks)["status"] == "valid")
            note = notes[case_id]
            if note["case_type"] == "control":
                _require(note["control_case_id"] == case_id and note["changes"] == [])
                continue
            control_id = note["control_case_id"]
            _require(
                control_id in notes and notes[control_id]["case_type"] == "control"
            )
            _require(note["source_item_id"] == notes[control_id]["source_item_id"])
            control = cases[control_id]
            actual = {key for key in item if item[key] != control[key]}
            _require(
                actual
                in ({"question"}, {"answer"}, {"source"}, {"page"}, {"source", "page"})
            )
            changes = note["changes"]
            _require(len({c["field"] for c in changes}) == len(changes))
            _require({c["field"] for c in changes} == actual)
            for change in changes:
                field = change["field"]
                _require(
                    type(change["before"]) is type(control[field])
                    and type(change["after"]) is type(item[field])
                    and change["before"] == control[field]
                    and change["after"] == item[field]
                )
    except ChallengeError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise ChallengeError(
            "Invalid or unreadable challenge inputs/annotations."
        ) from None
    return root.resolve(), inputs, annotations, snapshots


def _identity(run_id):
    return {"schema_version": 1, "record_kind": RECORD_KIND, "challenge_run_id": run_id}


def _new_directory(parent):
    parent = Path(parent).expanduser().absolute()
    _require(not parent.is_symlink(), "Challenge output parent must not be a symlink.")
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for _ in range(10):
            target = parent / str(uuid4())
            try:
                target.mkdir(mode=0o700)
                return target.resolve()
            except FileExistsError:
                continue
    except OSError:
        pass
    raise ChallengeError("Cannot create a new challenge recording directory.")


class Recording:
    """Independent artifact allowlist; no mutation of formal judge recorder state."""

    def __init__(self, root):
        self.root, self.events, self.failed = root, [], False

    def write(self, name, content):
        temporary = None
        try:
            _require(not self.failed and name in ARTIFACTS | {"complete.json"})
            target = self.root / name
            _require(not target.exists() and not target.is_symlink())
            raw = content if isinstance(content, bytes) else content.encode("utf-8")
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as out:
                temporary = Path(out.name)
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, target)
        except (Exception, KeyboardInterrupt):  # noqa: BLE001 - fail-closed recording boundary
            self.failed = True
            raise ChallengeError(
                "Challenge artifact recording failed; no further calls allowed."
            ) from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def emit(self, kind, **payload):
        try:
            _require(not self.failed)
            event = {
                **_identity(self.root.name),
                "seq": len(self.events) + 1,
                "time": datetime.now(UTC).isoformat(),
                "type": kind,
                **payload,
            }
            data = runner.canonical(event) + "\n"
            path = self.root / "calls.jsonl"
            _require(not path.is_symlink())
            fd = os.open(path, os.O_WRONLY | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            self.events.append(json.loads(data))
        except (Exception, KeyboardInterrupt):  # noqa: BLE001 - fail-closed recording boundary
            self.failed = True
            raise ChallengeError(
                "Challenge event recording failed; no further calls allowed."
            ) from None


def _labels(row):
    labels = {}
    for dimension in DIMENSIONS:
        phase = row["answerability" if dimension == "answerability" else "support"]
        labels[dimension] = (
            (phase["model_review"] or {}).get(
                "verdict" if dimension == "answerability" else dimension
            )
            if phase["status"] == "evaluated"
            else phase["status"]
        )
    return labels


def _queue(rows, run_id):
    return [
        {
            **_identity(run_id),
            "case_id": row["case_id"],
            "phase": phase,
            "status": "pending",
            "reasons": deepcopy(row[phase]["human_review"]["reasons"]),
            "model_review": deepcopy(row[phase]["model_review"]),
            "evidence_records": deepcopy(
                row[phase]["verification"]["evidence_records"]
            ),
        }
        for row in rows
        for phase in PHASES
        if row[phase]["human_review"]["status"] == "pending"
    ]


def summarize(rows, events, notes, *, run_id, suite_id, mode, max_items):
    selected = rows[:max_items]
    queue = _queue(rows, run_id)
    starts = [e for e in events if e["type"] == "call_started"]
    finishes = [e for e in events if e["type"] == "call_finished"]
    finish_ids = {e["call_id"] for e in finishes}
    unresolved = [e["call_id"] for e in starts if e["call_id"] not in finish_ids]
    dimensions = {
        dimension: dict.fromkeys((*labels, "error", "not_evaluated"), 0)
        for dimension, labels in DIMENSIONS.items()
    }
    for row in rows:
        for dimension, label in _labels(row).items():
            dimensions[dimension][label] += 1
    return {
        **_identity(run_id),
        "suite_id": suite_id,
        "mode": mode,
        "calibration_status": "not_calibrated",
        "case_count": len(rows),
        "control_count": sum(n["case_type"] == "control" for n in notes.values()),
        "mutant_count": sum(n["case_type"] == "mutant" for n in notes.values()),
        "selected_case_count": len(selected),
        "selected_phase_count": len(selected) * 2,
        "selected_evaluated_phase_count": sum(
            r[p]["status"] == "evaluated" for r in selected for p in PHASES
        ),
        "evaluated_phase_count": sum(
            r[p]["status"] == "evaluated" for r in rows for p in PHASES
        ),
        "planned_phase_count": len(rows) * 2,
        "complete_model_review_case_count": sum(
            all(r[p]["status"] == "evaluated" for p in PHASES) for r in rows
        ),
        "model_all_positive_case_count": sum(
            list(_labels(r).values()) == ["answerable", "supported", "supported"]
            for r in rows
        ),
        "technical_error_phase_count": sum(
            r[p]["status"] == "error" for r in rows for p in PHASES
        ),
        "human_review_pending_case_count": len({entry["case_id"] for entry in queue}),
        "human_review_pending_phase_count": len(queue),
        "human_review_reason_counts": dict(
            Counter(reason["code"] for entry in queue for reason in entry["reasons"])
        ),
        "dimension_counts": dimensions,
        "call_starts": len(starts),
        "call_finishes": len(finishes),
        "response_count": sum(e["status"] == "response" for e in finishes),
        "retry_count": sum(e["attempt"] > 1 for e in starts),
        "unresolved_calls": unresolved,
        "usage": _usage(finishes, unresolved),
        "cost": None,
    }


def _state_text(row):
    labels = _labels(row)
    return "; ".join(
        [f"{key}={value}" for key, value in labels.items()]
        + [
            f"{phase}: technical={row[phase]['technical_status']}, human={row[phase]['human_review']['status']}"
            for phase in PHASES
        ]
    )


def render_report(metrics, rows, notes):
    lines = [
        "# Judge 人工挑战试验",
        "",
        "**人工设计 challenge；尚未校准；不是正式生成评测或 golden 结果。**",
        "模型原判、技术状态和人工状态相互独立；不自动判定设计目标是否命中。",
        "control 是原样基线，不是预设通过真值；设计目标不是模型评审结果。",
        "",
        "Suite：",
        *_block(metrics["suite_id"]),
        "",
        "状态：",
        *_block(metrics["run_status"] + " / " + metrics["record_integrity"]),
        "",
    ]
    if metrics["mode"] == "prepare":
        lines += ["**仅离线准备，没有调用模型，所有语义未评。**", ""]
    lines += ["## 覆盖与执行", "", "| 项目 | 结果 |", "| --- | ---: |"]
    for label, value in [
        (
            "全部案例 / control / mutant",
            f"{metrics['case_count']} / {metrics['control_count']} / {metrics['mutant_count']}",
        ),
        (
            "选中完整评审 / 选中计划",
            f"{metrics['selected_evaluated_phase_count']} / {metrics['selected_phase_count']}",
        ),
        (
            "全量完整评审 / 全量计划",
            f"{metrics['evaluated_phase_count']} / {metrics['planned_phase_count']}",
        ),
        ("模型三维正向案例数（不是通过数）", metrics["model_all_positive_case_count"]),
        ("技术错误阶段", metrics["technical_error_phase_count"]),
        ("待人工复核案例", metrics["human_review_pending_case_count"]),
        (
            "调用尝试 / 响应 / 重试",
            f"{metrics['call_starts']} / {metrics['response_count']} / {metrics['retry_count']}",
        ),
    ]:
        lines.append(f"| {label} | {value} |")
    lines += [
        "",
        "完整评审包括负判、未知和待人工；未选案例保留但不阻止选中任务完成。",
        "调用尝试不证明请求送达或计费；响应数也不等于完整评审数。",
        "",
        "## 逐案例模型结果",
        "",
    ]
    for ordinal, row in enumerate(rows, 1):
        note = notes[row["case_id"]]
        lines += [
            f"### 案例 {ordinal}",
            "",
            *_block(row["case_id"]),
            *_block(
                f"设计类型={note['case_type']}; 配对control={note['control_case_id']}"
            ),
            "",
            "题干：",
            *_block(row["input"]["question"]),
            "",
            "答案：",
            *_block(row["input"]["answer"]),
            "",
            "最终引用：",
            *_block(f"{row['input']['source']} / {row['input']['page']}"),
            "",
            "真实模型原判及状态：",
            *_block(_state_text(row)),
            "",
        ]
        for phase in PHASES:
            state = row[phase]
            lines += [f"**{phase}**", "", *_block(state.get("reason")), ""]
            review = state.get("model_review")
            if review is None:
                lines += ["没有完整模型结论；不以设计目标补填。", ""]
                if state.get("raw_response") is not None:
                    lines += [
                        "不完整原始可见响应：",
                        *_block(state["raw_response"]),
                        "",
                    ]
            else:
                lines += ["模型说明：", *_block(review["reason"]), ""]
                for claim in review.get("claims", []):
                    lines += [
                        "模型论断：",
                        *_block(claim["text"]),
                        *_block(claim["verdict"]),
                        "",
                    ]
            lines += ["人工待办原因：", ""]
            _diagnostics(lines, state["human_review"]["reasons"])
            lines += ["来源与定位诊断：", ""]
            _diagnostics(lines, state["verification"]["diagnostics"])
            _evidence(lines, state["verification"]["evidence_records"])
    lines += [
        "## Control / mutant 配对对照",
        "",
        "下列设计目标为人工设计意图，不是模型结果或已校准真值。",
        "",
    ]
    lookup = {r["case_id"]: r for r in rows}
    for ordinal, row in enumerate(rows, 1):
        note = notes[row["case_id"]]
        if note["case_type"] != "mutant":
            continue
        control = lookup[note["control_case_id"]]
        lines += [
            f"### 配对 {ordinal}",
            "",
            *_block(f"control={control['case_id']} | mutant={row['case_id']}"),
            "control 实际模型结果（未执行则未评）：",
            *_block(_state_text(control)),
            "",
            "mutant 实际模型结果：",
            *_block(_state_text(row)),
            "",
            "人工修改理由：",
            *_block(note["design_reason"]),
            "",
            "人工设计目标（非模型结果）：",
            *_block(note["qualitative_target"]["goal"]),
            *_block(note["qualitative_target"]["label_policy"]),
            "",
        ]
        lines += [
            "模型原判与状态并列（不是预期标签）：",
            "",
            "| 维度／状态 | control | mutant |",
            "| --- | --- | --- |",
        ]
        for dimension in DIMENSIONS:
            lines.append(
                f"| {dimension} | {_labels(control)[dimension]} | {_labels(row)[dimension]} |"
            )
        for phase in PHASES:
            lines.append(
                f"| {phase} 技术 | {control[phase]['technical_status']} | {row[phase]['technical_status']} |"
            )
            lines.append(
                f"| {phase} 人工 | {control[phase]['human_review']['status']} | {row[phase]['human_review']['status']} |"
            )
        lines.append("")
        for change in note["changes"]:
            lines += [
                "修改字段：",
                *_block(change["field"]),
                "修改前：",
                *_block(change["before"]),
                "修改后：",
                *_block(change["after"]),
                "",
            ]
    lines += [
        "## 边界",
        "",
        "本报告不计算预期命中率、检测成功率或准确率，不替人工作最终裁决。",
        "该独立记录不支持正式 judge-replay；本版本没有 challenge replay/resume。",
        "",
    ]
    return "\n".join(lines)


def run_challenge(
    suite_dir,
    *,
    execute=False,
    max_calls=0,
    max_items=None,
    output_dir=None,
    settings=None,
    client_factory=None,
):
    """Prepare by default; execute only after validating the complete independent suite."""
    _require(
        type(max_calls) is int and max_calls >= 0, "max-calls must be nonnegative."
    )
    _require(
        max_items is None or type(max_items) is int and max_items > 0,
        "max-items must be positive.",
    )
    _require(
        not execute or max_calls > 0,
        "--execute requires an explicit positive --max-calls budget.",
    )
    root, inputs, annotations, snapshots = load_suite(suite_dir)
    if execute:
        try:
            settings = settings or judge.JudgeSettings()
        except (ValidationError, ValueError):
            raise ChallengeError("Invalid independent JUDGE configuration.") from None
    target = _new_directory(
        output_dir if output_dir is not None else Path.cwd() / "outputs/eval/challenges"
    )
    record, mode = Recording(target), "execute" if execute else "prepare"
    manifest = {
        **_identity(target.name),
        "protocol_version": PROTOCOL_VERSION,
        "judge_protocol_version": judge.PROTOCOL_VERSION,
        "judge_protocol_digest": judge.protocol_digest(),
        "checker_identity": {
            **runner._checker_identity(),
            "challenge.py": runner.digest(Path(__file__).read_bytes()),
        },
        "suite_dir": str(root),
        "suite_id": inputs["suite_id"],
        "snapshot_sha256": {
            name: runner.digest(raw) for name, raw in snapshots.items()
        },
        "created_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "max_calls": max_calls,
        "max_items": max_items,
        "judge_config": judge.safe_identity(settings) if execute else None,
        "calibration": "not_calibrated",
        "cache_policy": "no_reuse",
    }
    for name, raw in snapshots.items():
        record.write(name, raw)
    record.write("manifest.json", runner.canonical(manifest) + "\n")
    record.write("calls.jsonl", "")
    rows, used, client, interrupted = [], 0, None, False
    for index, case in enumerate(inputs["cases"]):
        row = {
            **_identity(target.name),
            "case_id": case["case_id"],
            "input": deepcopy(case["input"]),
        }
        item = case["input"]
        for phase in PHASES:
            reason = (
                "prepare"
                if not execute
                else "max_items"
                if max_items is not None and index >= max_items
                else "interrupted"
                if interrupted
                else None
            )
            request = None
            if reason is None:
                allowed = (
                    ("question",)
                    if phase == "answerability"
                    else ("question", "answer", "source", "page")
                )
                request = judge.build_request(
                    phase,
                    {key: item[key] for key in allowed},
                    inputs["chunks"],
                    settings,
                )
                if runner._request_size(request) > settings.max_input_chars:
                    reason = "input_limit"
            decision = runner._phase("not_evaluated", reason)
            if reason is None:
                for attempt in range(1, settings.max_attempts + 1):
                    if used >= max_calls:
                        if decision["status"] != "error":
                            decision = runner._phase("not_evaluated", "call_budget")
                        break
                    if client is None:
                        try:
                            client = (client_factory or judge.make_client)(settings)
                        except Exception:  # noqa: BLE001 - no SDK/config details in error output
                            raise ChallengeError(
                                "Challenge client initialization failed."
                            ) from None
                    call_id = str(uuid4())
                    record.emit(
                        "call_started",
                        call_id=call_id,
                        case_id=row["case_id"],
                        phase=phase,
                        attempt=attempt,
                        request=request,
                    )
                    used += 1
                    started, retry = monotonic(), False
                    try:
                        response = judge.invoke(client, deepcopy(request))
                        finish = {
                            "status": "response",
                            "response": response,
                            "error_kind": None,
                            "error": None,
                        }
                    except KeyboardInterrupt as exc:
                        interrupted = True
                        finish = {
                            "status": "interrupted",
                            "response": None,
                            "error_kind": "interrupted",
                            "error": safe_error(exc),
                        }
                    except Exception as exc:  # noqa: BLE001 - provider errors are safely categorized
                        retry = runner._transient(exc)
                        finish = {
                            "status": "error",
                            "response": None,
                            "error_kind": "transport_error"
                            if retry
                            else "provider_error",
                            "error": safe_error(exc),
                        }
                    record.emit(
                        "call_finished",
                        call_id=call_id,
                        case_id=row["case_id"],
                        phase=phase,
                        attempt=attempt,
                        elapsed_seconds=monotonic() - started,
                        **finish,
                    )
                    decision = runner._classify(phase, finish, item, inputs["chunks"])
                    if (
                        not (retry or decision["reason"] == "schema_error")
                        or interrupted
                    ):
                        break
            row[phase] = decision
            record.emit(
                "task_finished", case_id=row["case_id"], phase=phase, result=decision
            )
        rows.append(row)
    notes = {note["case_id"]: note for note in annotations["cases"]}
    metrics = summarize(
        rows,
        record.events,
        notes,
        run_id=target.name,
        suite_id=inputs["suite_id"],
        mode=mode,
        max_items=max_items,
    )
    status = (
        "interrupted"
        if interrupted
        else "prepared"
        if not execute
        else "completed"
        if metrics["selected_evaluated_phase_count"] == metrics["selected_phase_count"]
        else "incomplete"
    )
    metrics.update(
        run_status=status,
        record_integrity="complete",
        protocol_version=PROTOCOL_VERSION,
    )
    record.write("judgments.jsonl", "".join(runner.canonical(r) + "\n" for r in rows))
    record.write(
        "review_queue.jsonl",
        "".join(runner.canonical(r) + "\n" for r in _queue(rows, target.name)),
    )
    record.write("metrics.json", runner.canonical(metrics) + "\n")
    record.write("report.md", render_report(metrics, rows, notes))
    hashes = {
        name: runner.digest((target / name).read_bytes()) for name in sorted(ARTIFACTS)
    }
    record.write(
        "complete.json",
        runner.canonical(
            {**_identity(target.name), "status": status, "artifacts": hashes}
        )
        + "\n",
    )
    return {
        "challenge_dir": str(target),
        "report_path": str(target / "report.md"),
        "status": status,
        "metrics": metrics,
        "exit_code": 130 if interrupted else 1 if status == "incomplete" else 0,
    }
