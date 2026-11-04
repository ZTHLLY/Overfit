"""Provider-independent MCQ planning, deduplication, and presentation policy."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

LABELS = ("A", "B", "C", "D")
MAX_BATCH_QUESTIONS = 10
MAX_BATCH_DISPATCHES = 3


@dataclass(frozen=True)
class BatchPlan:
    index: int
    question_count: int


@dataclass(frozen=True)
class DeduplicationResult:
    items: list[Any]
    removed: list[dict[str, int]]


@dataclass(frozen=True)
class BalanceResult:
    items: list[Any]
    mappings: list[dict[str, str]]
    seed_identity: str


class BatchGenerationError(RuntimeError):
    """A batch failed; complete earlier batches remain checkpointed."""

    def __init__(self, batch_index: int, completed_batches: int):
        self.batch_index = batch_index
        self.completed_batches = completed_batches
        super().__init__(f"MCQ batch {batch_index} failed.")


def plan_batches(question_count: int) -> list[BatchPlan]:
    """Split a positive requested count into ordered batches of at most ten."""
    if type(question_count) is not int or question_count <= 0:
        raise ValueError("Question count must be a positive integer.")
    batches = []
    remaining = question_count
    while remaining:
        batches.append(
            BatchPlan(
                index=len(batches) + 1,
                question_count=min(remaining, MAX_BATCH_QUESTIONS),
            )
        )
        remaining -= batches[-1].question_count
    return batches


def run_batches(plans, generate, *, checkpoint=None) -> list[Any]:
    """Run ordered batches fail-closed with a fixed three-dispatch allowance.

    ``generate`` owns provider retries but receives the immutable call ceiling.
    This coordinator never retries a whole batch and never starts a later batch
    after a failure.  The checkpoint callback is invoked before every dispatch
    and after its terminal batch state.
    """
    plans = list(plans)
    if not plans or any(not isinstance(batch, BatchPlan) for batch in plans):
        raise ValueError("At least one valid batch plan is required.")
    checkpoint = checkpoint or (lambda state: None)
    checkpoint(
        {
            "batch_index": 0,
            "question_count": sum(batch.question_count for batch in plans),
            "state": "planned",
        }
    )
    items: list[Any] = []
    for completed, batch in enumerate(plans):
        state = {
            "batch_index": batch.index,
            "question_count": batch.question_count,
        }
        checkpoint({**state, "state": "running"})
        try:
            generated = generate(batch, max_calls=MAX_BATCH_DISPATCHES)
        except (Exception, KeyboardInterrupt) as exc:
            checkpoint({**state, "state": "failed"})
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise BatchGenerationError(batch.index, completed) from None
        if not isinstance(generated, list):
            checkpoint({**state, "state": "failed"})
            raise BatchGenerationError(batch.index, completed)
        items.extend(generated)
        checkpoint({**state, "state": "completed"})
    return items


def partition_material(
    chunks: list[Any],
    plans: list[BatchPlan],
    *,
    reuse: bool,
    max_per_batch: int = 30,
) -> list[list[Any]]:
    """Freeze a bounded material list for every planned batch.

    Topic mode reuses its one reranked selection.  Whole-course mode splits a
    frozen selection evenly, capped at the largest 3:1 evaluation track.  If
    there are fewer chunks than batches, reuse avoids dispatching an empty
    context while retaining the same selected identities.
    """
    if not chunks or not plans:
        raise ValueError("Material and batch plans must be non-empty.")
    if type(max_per_batch) is not int or max_per_batch <= 0:
        raise ValueError("Material batch limit must be positive.")
    selected = list(chunks[:max_per_batch])
    if reuse or len(chunks) < len(plans):
        return [list(selected) for _ in plans]
    available = list(chunks[: len(plans) * max_per_batch])
    base, remainder = divmod(len(available), len(plans))
    result = []
    start = 0
    for index in range(len(plans)):
        size = base + (1 if index < remainder else 0)
        result.append(available[start : start + size])
        start += size
    return result


def _dump(item: Any) -> dict:
    if isinstance(item, dict):
        return json.loads(json.dumps(item, ensure_ascii=False))
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="python")
    raise TypeError("MCQ item must be a mapping or Pydantic model.")


def _restore(original: Any, value: dict) -> Any:
    if isinstance(original, dict):
        return value
    validator = getattr(type(original), "model_validate", None)
    if validator is None:
        raise TypeError("MCQ item type cannot be reconstructed.")
    return validator(value)


def _text_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _duplicate_key(item: Any) -> tuple[str, str]:
    value = _dump(item)
    correct = value["correct_option"]
    return _text_key(value["question"]), _text_key(value["options"][correct]["text"])


def _evidence_score(item: Any) -> tuple[int, int]:
    """Prefer more complete visible feedback, then a stable citation-range count."""
    value = _dump(item)
    explanations = sum(
        len(option["explanation"].strip()) for option in value["options"].values()
    )
    ranges = sum(option.get("page_end") is not None for option in value["options"].values())
    return explanations, ranges


def deduplicate_items(items: list[Any]) -> DeduplicationResult:
    """Remove only exact normalized question/correct-answer equivalents.

    Deliberately avoiding a fuzzy threshold keeps related questions unless both
    the stem and materially correct answer are text-equivalent.  No top-up is
    attempted here or by this module.
    """
    kept: list[Any] = []
    first_by_key: dict[tuple[str, str], tuple[int, int, tuple[int, int]]] = {}
    removed: list[dict[str, int]] = []
    for index, item in enumerate(items, 1):
        key = _duplicate_key(item)
        if key in first_by_key:
            kept_index, position, score = first_by_key[key]
            current_score = _evidence_score(item)
            if current_score > score:
                kept[position] = item
                first_by_key[key] = (index, position, current_score)
                removed.append({"kept_index": index, "removed_index": kept_index})
            else:
                removed.append({"kept_index": kept_index, "removed_index": index})
        else:
            first_by_key[key] = (index, len(kept), _evidence_score(item))
            kept.append(item)
    return DeduplicationResult(items=kept, removed=removed)


def _seed_int(seed_identity: str) -> int:
    if not isinstance(seed_identity, str) or not seed_identity:
        raise ValueError("A non-empty stable seed identity is required.")
    return int.from_bytes(hashlib.sha256(seed_identity.encode()).digest()[:8], "big")


def balance_options(items: list[Any], *, seed_identity: str) -> BalanceResult:
    """Balance correct letters while moving every option object atomically."""
    offset = _seed_int(seed_identity) % len(LABELS)
    balanced: list[Any] = []
    mappings: list[dict[str, str]] = []
    for index, original in enumerate(items):
        value = _dump(original)
        old_correct = value["correct_option"]
        target = LABELS[(index + offset) % len(LABELS)]
        old_order = [old_correct, *(label for label in LABELS if label != old_correct)]
        new_order = [target, *(label for label in LABELS if label != target)]
        mapping = dict(zip(old_order, new_order, strict=True))
        moved = {
            new_label: value["options"][old_label]
            for old_label, new_label in mapping.items()
        }
        value["options"] = {label: moved[label] for label in LABELS}
        value["correct_option"] = target
        balanced.append(_restore(original, value))
        mappings.append(mapping)
    return BalanceResult(
        items=balanced,
        mappings=mappings,
        seed_identity=seed_identity,
    )


def mix_items(items: list[Any], *, seed_identity: str) -> list[Any]:
    """Return a stable mixed presentation order without changing item content."""
    _seed_int(seed_identity)

    def key(indexed: tuple[int, Any]) -> tuple[str, int]:
        index, item = indexed
        payload = json.dumps(_dump(item), ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(
            f"{seed_identity}\0{index}\0{payload}".encode()
        ).hexdigest()
        return digest, index

    return [item for _, item in sorted(enumerate(items), key=key)]
