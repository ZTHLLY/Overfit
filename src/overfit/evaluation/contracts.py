"""Versioned file boundaries, independent of runtime configuration."""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from overfit.errors import OverfitError


class TraceWriteError(OverfitError):
    """Recording failed; callers must not send another model request."""


class TraceReadError(OverfitError):
    """A recording cannot be safely interpreted."""


class _Versioned(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def _exact_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported trace schema version")
        return value


def _utc_time(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("Trace timestamps must include UTC timezone")
    return value


class Event(_Versioned):
    run_id: str = Field(min_length=1)
    seq: int = Field(ge=1)
    event_type: Literal[
        "run_started",
        "materials_selected",
        "call_started",
        "call_finished",
        "validation_finished",
        "citation_processed",
        "run_finished",
    ]
    time: str = Field(min_length=1)
    payload: dict

    _validate_time = field_validator("time")(_utc_time)


class Manifest(_Versioned):
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    started_at: str = Field(min_length=1)
    task: dict
    config: dict
    code_identity: dict

    _validate_time = field_validator("started_at")(_utc_time)
