"""Fail-closed local trace writer; no settings, model or storage imports."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from overfit.evaluation.contracts import Event, Manifest, TraceWriteError


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _snapshot(value: object):
    return json.loads(_json(value))


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def safe_error(exc: BaseException) -> dict:
    """Never copy SDK exception strings, which may contain credentials."""
    if isinstance(exc, KeyboardInterrupt):
        kind, message = "interrupted", "Operation interrupted."
    elif isinstance(exc, TraceWriteError):
        kind, message = "trace_write_error", "Trace recording failed."
    elif isinstance(exc, TimeoutError):
        kind, message = "timeout", "Operation timed out."
    elif isinstance(exc, OSError):
        kind, message = "io_error", "An I/O operation failed."
    elif isinstance(exc, (ValueError, TypeError)):
        kind, message = "validation_error", "Data validation failed."
    else:
        kind, message = "operation_error", "Operation failed."
    return {"type": kind, "message": message}


def safe_endpoint(url: str) -> str:
    """Retain a useful service identity, never URL credentials or queries."""
    if not isinstance(url, str):
        return "unknown"
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "unknown"
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except (ValueError, TypeError):
        return "unknown"


def _safe_config(config: dict) -> dict:
    scalar_keys = {
        "generation_model",
        "embedding_model",
        "llm_model",
        "embed_model",
        "temperature",
        "request_timeout",
        "timeout",
        "max_attempts",
        "generation_endpoint",
        "embedding_endpoint",
        "llm_endpoint",
        "embed_endpoint",
    }
    result = {}
    for key in scalar_keys & config.keys():
        value = config[key]
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = safe_endpoint(value) if key.endswith("endpoint") else value
    profile = config.get("index_profile")
    if isinstance(profile, dict):
        result["index_profile"] = {
            key: value
            for key, value in profile.items()
            if key
            in {
                "schema_version",
                "embed_model",
                "embed_dim",
                "chunk_size",
                "chunk_overlap",
                "parser",
            }
            and (isinstance(value, (str, int, float, bool)) or value is None)
        }
    for key in ("llm", "embed", "generation", "embedding"):
        value = config.get(key)
        if isinstance(value, dict):
            result[key] = {
                k: safe_endpoint(v) if k in {"base_url", "endpoint"} else v
                for k, v in value.items()
                if k
                in {
                    "model",
                    "base_url",
                    "endpoint",
                    "temperature",
                    "timeout",
                    "request_timeout",
                    "max_attempts",
                }
                and (isinstance(v, (str, int, float, bool)) or v is None)
            }
    return result


def collect_code_identity(repo: Path) -> dict:
    """Hash only source/templates and dependency identity, never env files."""
    repo = repo.resolve()
    hashes = {}
    candidates = [repo / "pyproject.toml", repo / "uv.lock"]
    source = repo / "src" / "overfit"
    if not source.is_symlink():
        candidates.extend(source.rglob("*.py"))
        candidates.extend(source.rglob("*.j2"))
    for path in sorted(candidates):
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(repo)
        ):
            continue
        hashes[path.relative_to(repo).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=normal"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()
        )
        reason = None
    except (OSError, subprocess.SubprocessError):
        head, dirty, reason = None, None, "Git identity unavailable."
    return {"git_head": head, "dirty": dirty, "files": hashes, "reason": reason}


class TraceWriter:
    """Append only until commit or first error; failed writers never resume."""

    def __init__(self, run_dir: Path, run_id: str):
        self.run_dir = run_dir
        self.run_id = run_id
        self.events: list[dict] = []
        self.last_call_id: str | None = None
        self.failed = False
        self.artifact_failed = False
        self.finished = False
        self.artifacts: dict[str, str] = {}
        self.manifest: dict = {}

    @classmethod
    def create(
        cls,
        outputs_dir: Path,
        task: dict,
        config: dict,
        code_identity: dict | None = None,
    ) -> TraceWriter:
        writer = None
        try:
            task = _snapshot(task)
            if type(task.get("questions")) is not int or task["questions"] <= 0:
                raise ValueError("Invalid question count")
            budget = task.get("material_budget", 0)
            if type(budget) is not int or budget < 0:
                raise ValueError("Invalid material budget")
            task = {
                key: task.get(key)
                for key in (
                    "course",
                    "topic",
                    "questions",
                    "material_budget",
                    "task_id",
                )
            }
            if not isinstance(task["course"], str) or not task["course"]:
                raise ValueError("Invalid course")
            task["material_budget"] = budget
            task["task_id"] = (
                task["task_id"]
                or hashlib.sha256(
                    _json({k: v for k, v in task.items() if k != "task_id"}).encode()
                ).hexdigest()[:24]
            )
            root = Path(outputs_dir) / "eval"
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            for _ in range(10):
                run_id = str(uuid4())
                run_dir = root / run_id
                try:
                    run_dir.mkdir(mode=0o700)
                    break
                except FileExistsError:
                    continue
            else:
                raise OSError("Unable to allocate unique run directory")
            writer = cls(run_dir, run_id)
            writer.manifest = Manifest(
                run_id=run_id,
                task_id=task["task_id"],
                started_at=_now(),
                task=task,
                config=_safe_config(config),
                code_identity=code_identity
                or {
                    "git_head": None,
                    "dirty": None,
                    "files": {},
                    "reason": "Code identity not supplied.",
                },
            ).model_dump(mode="json")
            writer._write_atomic("manifest.json", _json(writer.manifest) + "\n")
            writer.emit("run_started", {"task": task})
            return writer
        except Exception:  # noqa: BLE001 - fail closed without leaking input/SDK details
            if writer is not None:
                writer.failed = True
            raise TraceWriteError("Could not initialize trace recording.") from None

    def _check_open(self, *, allow_failure_terminal: bool = False):
        if self.failed or self.finished:
            raise TraceWriteError("Trace writer is failed or already finished.")
        if self.artifact_failed and not allow_failure_terminal:
            raise TraceWriteError(
                "Artifact recording failed; only failure finalization is allowed."
            )

    def emit(self, event_type: str, payload: dict) -> None:
        self._check_open(
            allow_failure_terminal=(
                event_type == "run_finished"
                and isinstance(payload, dict)
                and payload.get("status") in {"failed", "interrupted"}
            )
        )
        try:
            event = Event(
                run_id=self.run_id,
                seq=len(self.events) + 1,
                event_type=event_type,
                time=_now(),
                payload=_snapshot(payload),
            ).model_dump(mode="json")
            path = self.run_dir / "traces.jsonl"
            if path.is_symlink():
                raise ValueError("Invalid log path")
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "ab") as stream:
                stream.write((_json(event) + "\n").encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            self.events.append(event)
            if event_type == "run_finished":
                self.finished = True
        except KeyboardInterrupt:
            # A partially written line must never be followed by another event.
            self.failed = True
            raise
        except Exception:  # noqa: BLE001 - fail closed without leaking input/SDK details
            self.failed = True
            raise TraceWriteError("Could not persist trace event.") from None

    def start_call(self, attempt_index: int, format_index: int, request: dict) -> str:
        self._check_open()
        try:
            if any(type(i) is not int or i < 1 for i in (attempt_index, format_index)):
                raise ValueError("Invalid call index")
            call_id = str(uuid4())
            self.emit(
                "call_started",
                {
                    "call_id": call_id,
                    "attempt_index": attempt_index,
                    "format_index": format_index,
                    "request": _snapshot(request),
                },
            )
            self.last_call_id = call_id
            return call_id
        except Exception:  # noqa: BLE001 - fail closed without leaking input/SDK details
            self.failed = True
            raise TraceWriteError("Could not persist call intent.") from None

    def _write_atomic(self, name: str, content: str) -> None:
        temporary = None
        try:
            if (
                not name
                or name in {".", "..", "traces.jsonl"}
                or "/" in name
                or "\\" in name
                or Path(name).is_absolute()
            ):
                raise ValueError("Invalid artifact name")
            target = self.run_dir / name
            if target.exists() or target.is_symlink():
                raise ValueError("Artifact already exists")
            data = content.encode("utf-8")
            with tempfile.NamedTemporaryFile(
                dir=self.run_dir, prefix=".pending-", delete=False
            ) as out:
                temporary = Path(out.name)
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, target)
            self.artifacts[name] = hashlib.sha256(data).hexdigest()
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def write_artifact(self, name: str, content: str) -> None:
        self._check_open()
        try:
            if name == "manifest.json":
                raise ValueError("Manifest is immutable")
            self._write_atomic(name, content)
        except KeyboardInterrupt:
            self.artifact_failed = True
            raise
        except Exception:  # noqa: BLE001 - fail closed without leaking input/SDK details
            self.artifact_failed = True
            raise TraceWriteError("Could not persist trace artifact.") from None

    def finish(
        self,
        status: str,
        stage: str,
        generation_outcome: str | None,
        error: dict | None = None,
    ) -> None:
        self._check_open(allow_failure_terminal=status in {"failed", "interrupted"})
        try:
            if status not in {"completed", "empty", "failed", "interrupted"}:
                raise ValueError("Invalid run status")
            if generation_outcome not in {
                None,
                "not_started",
                "failed",
                "model_empty",
                "all_dropped",
                "retained",
            }:
                raise ValueError("Invalid generation outcome")
            required = {"manifest.json"}
            if status in {"completed", "empty"}:
                required.update({"result.json", "report.md"})
            if status == "completed" and not (
                any(n.endswith("_mock_exam.md") for n in self.artifacts)
                and any(n.endswith("_answers.md") for n in self.artifacts)
            ):
                raise ValueError("Missing exam artifacts")
            if not required <= self.artifacts.keys():
                raise ValueError("Missing required artifacts")
            started = [e for e in self.events if e["event_type"] == "call_started"]
            ended = [e for e in self.events if e["event_type"] == "call_finished"]
            finished_ids = {e["payload"].get("call_id") for e in ended}
            unresolved = sum(
                e["payload"]["call_id"] not in finished_ids for e in started
            )
            self.emit(
                "run_finished",
                {
                    "status": status,
                    "stage": stage,
                    "generation_outcome": generation_outcome,
                    "error": _snapshot(error),
                    "artifacts": dict(self.artifacts),
                    "counts": {
                        "call_started": len(started),
                        "call_finished": len(ended),
                        "unresolved": unresolved,
                        "call_count": None if unresolved else len(started),
                    },
                },
            )
            self.finished = True
        except Exception:  # noqa: BLE001 - fail closed without leaking input/SDK details
            # Event failure already poisons `failed`; missing/invalid artifacts
            # need not prevent one best-effort failure record on a healthy log.
            self.artifact_failed = True
            raise TraceWriteError("Could not commit trace run.") from None
