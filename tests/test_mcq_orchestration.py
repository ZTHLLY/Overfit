"""Deterministic, provider-free MCQ batch orchestration contracts."""

from copy import deepcopy

import pytest

from overfit.query.mcq import (
    BatchGenerationError,
    balance_options,
    deduplicate_items,
    mix_items,
    partition_material,
    plan_batches,
    run_batches,
)


def _item(number: int, *, correct: str = "A", question: str | None = None) -> dict:
    options = {
        label: {
            "text": f"{label} text {number}",
            "explanation": f"{label} explanation {number}",
            "source": f"week{number}.pdf",
            "page": number + 1,
            "page_end": None,
        }
        for label in "ABCD"
    }
    return {
        "topic": f"Topic {number % 3}",
        "question": question or f"Which statement describes concept {number}?",
        "options": options,
        "correct_option": correct,
    }


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(1, [1]), (10, [10]), (11, [10, 1]), (25, [10, 10, 5]), (30, [10, 10, 10])],
)
def test_plan_batches_never_exceeds_ten(requested, expected):
    assert [batch.question_count for batch in plan_batches(requested)] == expected
    assert [batch.index for batch in plan_batches(requested)] == list(
        range(1, len(expected) + 1)
    )


@pytest.mark.parametrize("requested", [0, -1, True, 1.5])
def test_plan_batches_rejects_nonpositive_or_noninteger_counts(requested):
    with pytest.raises(ValueError):
        plan_batches(requested)


def test_conservative_dedup_removes_only_clear_equivalents_without_top_up():
    first = _item(1, question="Which statement describes overfitting?")
    duplicate = deepcopy(first)
    duplicate["question"] = " Which statement   describes OVERFITTING? "
    related = _item(2, question="How can validation data reveal overfitting?")

    result = deduplicate_items([first, duplicate, related])

    assert result.items == [first, related]
    assert result.removed == [{"kept_index": 1, "removed_index": 2}]


def test_duplicate_with_more_complete_feedback_replaces_weaker_item():
    weak = _item(1, question="Which statement describes overfitting?")
    strong = deepcopy(weak)
    strong["options"]["B"]["explanation"] += " Additional grounded correction."

    result = deduplicate_items([weak, strong])

    assert result.items == [strong]
    assert result.removed == [{"kept_index": 2, "removed_index": 1}]


def test_balance_moves_complete_option_objects_and_is_deterministic():
    items = [_item(index, correct="A") for index in range(10)]
    originals = deepcopy(items)

    first = balance_options(items, seed_identity="run-123")
    second = balance_options(deepcopy(originals), seed_identity="run-123")

    assert first == second
    counts = {label: 0 for label in "ABCD"}
    for before, after, mapping in zip(originals, first.items, first.mappings, strict=True):
        counts[after["correct_option"]] += 1
        assert mapping[before["correct_option"]] == after["correct_option"]
        for old_label, new_label in mapping.items():
            assert after["options"][new_label] == before["options"][old_label]
    assert max(counts.values()) - min(counts.values()) <= 1


def test_mix_order_is_deterministic_and_not_topic_grouped():
    items = [_item(index) for index in range(12)]

    first = mix_items(items, seed_identity="run-123")
    second = mix_items(deepcopy(items), seed_identity="run-123")

    assert first == second
    assert sorted(item["question"] for item in first) == sorted(
        item["question"] for item in items
    )
    assert [item["topic"] for item in first] != sorted(
        item["topic"] for item in first
    )


def test_batch_runner_caps_every_batch_at_three_dispatches_and_checkpoints():
    calls = []
    checkpoints = []

    def generate(batch, *, max_calls):
        calls.append((batch.index, batch.question_count, max_calls))
        return [_item(batch.index)]

    result = run_batches(
        plan_batches(21), generate, checkpoint=checkpoints.append
    )

    assert calls == [(1, 10, 3), (2, 10, 3), (3, 1, 3)]
    assert [state["state"] for state in checkpoints] == [
        "planned",
        "running",
        "completed",
        "running",
        "completed",
        "running",
        "completed",
    ]
    assert len(result) == 3


def test_failed_batch_stops_later_dispatches_and_records_failure():
    calls = []
    checkpoints = []

    def generate(batch, *, max_calls):
        calls.append(batch.index)
        if batch.index == 2:
            raise RuntimeError("provider failed")
        return [_item(batch.index)]

    with pytest.raises(BatchGenerationError) as caught:
        run_batches(plan_batches(30), generate, checkpoint=checkpoints.append)

    assert calls == [1, 2]
    assert caught.value.batch_index == 2
    assert caught.value.completed_batches == 1
    assert checkpoints[-1] == {
        "batch_index": 2,
        "question_count": 10,
        "state": "failed",
    }


def test_whole_course_material_is_bounded_and_partitioned_per_batch():
    chunks = list(range(60))

    partitions = partition_material(chunks, plan_batches(30), reuse=False)

    assert [len(group) for group in partitions] == [20, 20, 20]
    assert [item for group in partitions for item in group] == chunks


def test_three_to_one_material_track_uses_all_ninety_chunks():
    chunks = list(range(90))

    partitions = partition_material(chunks, plan_batches(30), reuse=False)

    assert [len(group) for group in partitions] == [30, 30, 30]
    assert [item for group in partitions for item in group] == chunks


def test_too_few_whole_course_chunks_are_reused_instead_of_empty_batches():
    chunks = ["first", "second"]

    partitions = partition_material(chunks, plan_batches(30), reuse=False)

    assert partitions == [chunks, chunks, chunks]


def test_topic_material_can_be_reused_across_batches_without_exceeding_bound():
    chunks = list(range(10))

    partitions = partition_material(chunks, plan_batches(25), reuse=True)

    assert partitions == [chunks, chunks, chunks]
