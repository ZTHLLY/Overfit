"""Controlled, opt-in Generator fixtures with independent human annotations."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from overfit.errors import OverfitError
from overfit.evaluation.live import run_fixture_trace
from overfit.evaluation.trace import safe_endpoint
from overfit.models import Chunk

PROTOCOL_VERSION = "generator-challenge-v1"
RECORD_KIND = "generator_challenge"


class GeneratorChallengeError(OverfitError):
    """Only fixed, non-secret messages cross the CLI boundary."""


def _require(condition, message="Invalid generator challenge suite."):
    if not condition:
        raise GeneratorChallengeError(message)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    @field_validator("*", mode="after")
    @classmethod
    def nonblank(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("Text cannot be blank")
        return value


class _Chunk(_Strict):
    id: str
    text: str
    source: str
    page: int = Field(gt=0)
    page_end: int | None
    section: str | None
    provenance: dict | None

    @model_validator(mode="after")
    def ordered_page_range(self):
        if self.page_end is not None and self.page_end < self.page:
            raise ValueError("page_end cannot precede page")
        return self


class _Case(_Strict):
    case_id: str
    course: str
    topic: str
    questions: int = Field(gt=0)
    chunk_ids: list[str] = Field(min_length=1)


class _Inputs(_Strict):
    schema_id: Literal["overfit.generator_challenge_inputs.v1"] = Field(alias="schema")
    suite_id: str
    chunks: list[_Chunk] = Field(min_length=1)
    cases: list[_Case] = Field(min_length=1)


class _Annotation(_Strict):
    case_id: str
    case_type: Literal[
        "unrelated-only", "partial-context", "gold-hard-negative", "multi-source"
    ]
    trusted_chunk_ids: list[str]
    distractor_chunk_ids: list[str]
    design_reason: str
    expected_behaviors: list[str] = Field(min_length=1)
    unacceptable_behaviors: list[str] = Field(min_length=1)


class _Annotations(_Strict):
    schema_id: Literal["overfit.generator_challenge_annotations.v1"] = Field(
        alias="schema"
    )
    suite_id: str
    annotation_status: Literal["human_designed_not_model_results"]
    model_input_policy: str
    cases: list[_Annotation] = Field(min_length=1)


def _parse(raw: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result)
            result[key] = value
        return result

    def invalid(_):
        raise GeneratorChallengeError("Non-finite values are not allowed.")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def load_suite(suite_dir: Path):
    """Validate the full local suite without loading settings or providers."""
    root = Path(suite_dir).expanduser().absolute()
    try:
        _require(not root.is_symlink(), "Generator challenge symlinks are not supported.")
        snapshots = {}
        for name in ("inputs.json", "annotations.json"):
            path = root / name
            _require(
                not path.is_symlink(),
                "Generator challenge input symlinks are not supported.",
            )
            snapshots[name] = path.read_bytes()
        inputs = _parse(snapshots["inputs.json"])
        annotations = _parse(snapshots["annotations.json"])
        _Inputs.model_validate(inputs)
        _Annotations.model_validate(annotations)
        _require(inputs["suite_id"] == annotations["suite_id"])
        chunks = {item["id"]: item for item in inputs["chunks"]}
        cases = {item["case_id"]: item for item in inputs["cases"]}
        notes = {item["case_id"]: item for item in annotations["cases"]}
        _require(len(chunks) == len(inputs["chunks"]))
        _require(len(cases) == len(inputs["cases"]))
        _require(len(notes) == len(annotations["cases"]))
        _require(set(cases) == set(notes))
        # Construct every Chunk now so provenance and page ranges fail before selection.
        for item in chunks.values():
            Chunk(**item)
        for case_id, case in cases.items():
            ids = case["chunk_ids"]
            _require(len(ids) == len(set(ids)) and set(ids) <= set(chunks))
            note = notes[case_id]
            visible = set(ids)
            _require(
                len(note["trusted_chunk_ids"])
                == len(set(note["trusted_chunk_ids"]))
            )
            _require(
                len(note["distractor_chunk_ids"])
                == len(set(note["distractor_chunk_ids"]))
            )
            _require(set(note["trusted_chunk_ids"]) <= visible)
            _require(set(note["distractor_chunk_ids"]) <= visible)
            _require(
                not set(note["trusted_chunk_ids"])
                & set(note["distractor_chunk_ids"])
            )
    except GeneratorChallengeError:
        raise
    except (OSError, ValueError, TypeError, KeyError, ValidationError):
        raise GeneratorChallengeError(
            "Invalid or unreadable generator challenge inputs/annotations."
        ) from None
    return root.resolve(), inputs, annotations, snapshots


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _atomic(path: Path, content: str) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content.encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise GeneratorChallengeError(
            "Could not persist generator challenge recording."
        ) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _new_directory(output_dir: Path) -> Path:
    root = Path(output_dir).expanduser().absolute() / "eval" / "generator-challenges"
    try:
        _require(
            not root.is_symlink(), "Generator challenge output cannot be a symlink."
        )
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _require(
            not root.is_symlink(), "Generator challenge output cannot be a symlink."
        )
        for _ in range(10):
            target = root / str(uuid4())
            try:
                target.mkdir(mode=0o700)
                return target.resolve()
            except FileExistsError:
                continue
    except GeneratorChallengeError:
        raise
    except OSError:
        raise GeneratorChallengeError(
            "Could not allocate generator challenge output."
        ) from None
    raise GeneratorChallengeError("Could not allocate generator challenge output.")


def _report(data: dict) -> str:
    lines = [
        "# Generator challenge",
        "",
        f"- Status: **{data['status']}**",
        f"- Suite: `{data['suite_id']}`",
        f"- Mode: `{data['mode']}`",
        f"- Cases completed: {len(data['records'])}/{data['selected_count']}",
        f"- Provider calls: {data['call_count']}/{data['max_calls']}",
        "",
        "Human annotations are a separate snapshot and were not sent to the Generator.",
        "",
        "| Case | Status | Calls | Outcome | Retained | Trace |",
        "|---|---|---:|---|---:|---|",
    ]
    for row in data["records"]:
        summary = row.get("summary") or {}
        lines.append(
            f"| {row['case_id']} | {row['status']} | {row['call_count']} | "
            f"{summary.get('generation_outcome', '—')} | "
            f"{summary.get('retained_count', '—')} | {row.get('run_dir') or '—'} |"
        )
    return "\n".join(lines) + "\n"


def _persist(run_dir: Path, data: dict, *, complete: bool = False) -> None:
    _atomic(run_dir / "results.json", _canonical(data) + "\n")
    _atomic(run_dir / "report.md", _report(data))
    if complete:
        hashes = {
            name: _digest((run_dir / name).read_bytes())
            for name in ("inputs.json", "annotations.json", "manifest.json", "results.json", "report.md")
        }
        _atomic(
            run_dir / "complete.json",
            _canonical(
                {
                    "schema_version": 1,
                    "record_kind": RECORD_KIND,
                    "run_id": data["run_id"],
                    "status": data["status"],
                    "artifacts": hashes,
                }
            )
            + "\n",
        )


def run_generator_challenge(
    suite_dir: Path,
    *,
    execute: bool = False,
    max_calls: int = 0,
    max_items: int | None = None,
    max_attempts: int = 2,
    output_dir: Path = Path("outputs"),
    settings=None,
    trace_runner=run_fixture_trace,
    generator_factory=None,
) -> dict:
    """Prepare or sequentially execute a controlled Generator fixture suite."""
    _require(type(max_calls) is int and max_calls >= 0, "max-calls must be nonnegative.")
    _require(type(max_attempts) is int and max_attempts > 0, "max-attempts must be positive.")
    _require(
        max_items is None or (type(max_items) is int and max_items > 0),
        "max-items must be positive.",
    )
    if execute:
        _require(max_calls > 0, "--execute requires a positive --max-calls budget.")
        _require(settings is not None, "Generator settings are required for execution.")
        output_dir = settings.outputs_dir
    root, inputs, _annotations, snapshots = load_suite(suite_dir)
    selected = inputs["cases"][:max_items]
    run_dir = _new_directory(output_dir)
    run_id = run_dir.name
    for name, raw in snapshots.items():
        _atomic(run_dir / name, raw.decode())
    manifest = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "record_kind": RECORD_KIND,
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "execute" if execute else "prepare",
        "suite_dir": str(root),
        "suite_id": inputs["suite_id"],
        "snapshot_sha256": {name: _digest(raw) for name, raw in snapshots.items()},
        "max_calls": max_calls,
        "max_items": max_items,
        "max_attempts": max_attempts,
        "concurrency": 1,
        "generator_config": (
            {
                "base_url": safe_endpoint(settings.llm.base_url),
                "model": settings.llm.model,
                "temperature": settings.temperature,
                "request_timeout": settings.request_timeout,
            }
            if execute
            else None
        ),
    }
    _atomic(run_dir / "manifest.json", _canonical(manifest) + "\n")
    data = {
        "schema_version": 1,
        "record_kind": RECORD_KIND,
        "run_id": run_id,
        "suite_id": inputs["suite_id"],
        "mode": manifest["mode"],
        "status": "running" if execute else "prepared",
        "selected_count": len(selected),
        "max_calls": max_calls,
        "call_count": 0,
        "records": [],
        "failure": None,
    }
    _persist(run_dir, data)
    if not execute:
        _persist(run_dir, data, complete=True)
        return {**data, "run_dir": run_dir, "report_path": run_dir / "report.md", "exit_code": 0}

    chunks = {item["id"]: item for item in inputs["chunks"]}
    inputs_hash = _digest(snapshots["inputs.json"])
    for case in selected:
        remaining = max_calls - data["call_count"]
        if remaining <= 0:
            data["status"] = "incomplete"
            data["failure"] = {"case_id": case["case_id"], "category": "call_budget"}
            break
        ordered = [Chunk(**chunks[item]) for item in case["chunk_ids"]]
        identity = {
            "task_id": hashlib.sha256(
                f"{inputs['suite_id']}:{case['case_id']}".encode()
            ).hexdigest()[:24],
            "suite_id": inputs["suite_id"],
            "case_id": case["case_id"],
            "inputs_sha256": inputs_hash,
            "case_sha256": _digest(_canonical(case).encode()),
        }
        result = trace_runner(
            settings,
            course=case["course"],
            topic=case["topic"],
            questions=case["questions"],
            chunks=ordered,
            fixture_identity=identity,
            max_attempts=max_attempts,
            max_calls=remaining,
            generator_factory=generator_factory,
        )
        data["call_count"] += result["call_count"]
        data["records"].append(
            {
                "case_id": case["case_id"],
                "status": result["status"],
                "run_id": result["run_id"],
                "run_dir": result["run_dir"],
                "call_count": result["call_count"],
                "summary": result["summary"],
                "error": result["error"],
            }
        )
        _persist(run_dir, data)
        if result["status"] not in {"completed", "empty"}:
            data["status"] = (
                "interrupted" if result["status"] == "interrupted" else "incomplete"
            )
            data["failure"] = {
                "case_id": case["case_id"],
                "category": result["error"]["type"] if result["error"] else "generation",
            }
            break
    else:
        data["status"] = "completed"
    _persist(run_dir, data, complete=True)
    exit_code = 130 if data["status"] == "interrupted" else 1 if data["status"] == "incomplete" else 0
    return {
        **data,
        "run_dir": run_dir,
        "report_path": run_dir / "report.md",
        "exit_code": exit_code,
    }
