"""MCQ-v1 generator boundary and rendering contracts (offline only)."""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from overfit.config import LLMSettings
from overfit.models import (
    Chunk,
    GeneratedMCQExam,
    GeneratedMCQItem,
)
from overfit.query import generator as module
from overfit.query.generator import GenerationError, Generator, render_exam


def option(
    label: str,
    *,
    page: int | str = 17,
    page_end: int | str | None = None,
    source: str = "week3/notes.pdf",
) -> dict:
    return {
        "text": f"{label}. Choice {label}.",
        "explanation": f"Feedback for {label}.",
        "source": source,
        "page": page,
        "page_end": page_end,
    }


def item(**updates) -> dict:
    value = {
        "topic": "  Generalisation  ",
        "question": "Which statement best describes generalisation?",
        "options": {label: option(label) for label in "ABCD"},
        "correct_option": "B",
    }
    value.update(updates)
    return value


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        raw = next(self.responses)
        delta = SimpleNamespace(content=raw, reasoning_content=None)
        return iter([SimpleNamespace(choices=[SimpleNamespace(delta=delta)])])


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(
            temperature=0.25,
            request_timeout=123.0,
            max_retries=0,
            output_path=lambda course, filename: tmp_path / filename,
        ),
    )


def generator(raw: dict) -> Generator:
    instance = Generator(
        LLMSettings(_env_file=None, model="fixture-model", api_key="fixture")
    )
    instance._client = Client([json.dumps(raw)])
    return instance


def generator_responses(*raw_values: dict) -> Generator:
    instance = Generator(
        LLMSettings(_env_file=None, model="fixture-model", api_key="fixture")
    )
    instance._client = Client([json.dumps(value) for value in raw_values])
    return instance


def test_mock_exam_normalizes_only_mechanical_mcq_wire_variants():
    raw_item = item()
    raw_item["options"] = {label.lower(): option(label) for label in "ABCD"}
    raw_item["correct_option"] = "b"
    raw_item["options"]["a"]["page"] = "17-18"
    raw_item["options"]["c"]["page"] = "17"
    raw_item["options"]["d"].update(page=17, page_end="18")
    chunks = [Chunk("c1", "material", "week3/notes.pdf", 17, 18)]

    result = generator({"questions": [raw_item]}).mock_exam(
        "IFN580", chunks, count=1, max_attempts=1
    )

    question = result.exam.items[0]
    assert isinstance(question, GeneratedMCQItem)
    assert set(question.options) == set("ABCD")
    assert question.correct_option == "B"
    assert question.topic == "Generalisation"
    assert question.options["A"].text == "Choice A."
    assert (question.options["A"].page, question.options["A"].page_end) == (17, 18)
    assert (question.options["C"].page, question.options["C"].page_end) == (17, None)
    assert (question.options["D"].page, question.options["D"].page_end) == (17, 18)
    assert question.answer == "Choice B. Feedback for B."
    assert (question.source, question.page, question.page_end) == (
        "week3/notes.pdf", 17, None
    )


def test_batch_overproduction_is_invalid_and_consumes_retry_dispatch():
    first = {"questions": [item(), item(question="Which model is simpler?")]}
    second = {"questions": [item()]}
    instance = generator_responses(first, second)

    result = instance.mock_exam(
        "IFN580", [Chunk("c1", "material", "week3/notes.pdf", 17)],
        count=1, max_attempts=2, max_calls=3,
    )

    assert len(instance._client.calls) == 2
    assert result.attempts == 2
    assert len(result.exam.items) == 1


@pytest.mark.parametrize(
    "bad_item",
    [
        item(options={label: option(label) for label in "ABC"}),
        item(correct_option="E"),
        item(options={**{label: option(label) for label in "ABC"}, "D": option("C")}),
        item(options={**{label: option(label) for label in "ABC"}, "D": {**option("D"), "text": "None of the above."}}),
        item(question="Which statement is NOT correct?"),
    ],
    ids=["missing-D", "bad-correct-label", "duplicate-text", "none-above", "negative-stem"],
)
def test_mcq_structure_and_style_fail_closed(bad_item):
    with pytest.raises(ValidationError):
        GeneratedMCQExam.model_validate({"items": [bad_item]})


def test_positive_question_may_describe_a_not_available_condition():
    value = item(
        question="Which intrinsic metric applies when ground truth is not available?"
    )

    assert GeneratedMCQExam.model_validate({"items": [value]}).items


def test_internal_mcq_form_rejects_extra_fields_and_reversed_ranges():
    bad = item()
    bad["difficulty"] = "easy"
    with pytest.raises(ValidationError):
        GeneratedMCQExam.model_validate({"items": [bad]})

    bad = item()
    bad["options"]["A"]["confidence"] = 0.9
    with pytest.raises(ValidationError):
        GeneratedMCQExam.model_validate({"items": [bad]})

    bad = item()
    del bad["options"]["A"]["page_end"]
    with pytest.raises(ValidationError):
        GeneratedMCQExam.model_validate({"items": [bad]})

    bad = item()
    bad["options"]["A"].update(page=18, page_end=17)
    with pytest.raises(ValidationError):
        GeneratedMCQExam.model_validate({"items": [bad]})


def test_mcq_concision_boundaries_are_validated_locally():
    bad = item()
    bad["options"]["A"]["text"] = "One. Two. Three."
    with pytest.raises(ValidationError, match="at most two sentences"):
        GeneratedMCQExam.model_validate({"items": [bad]})

    bad = item()
    bad["options"]["A"]["explanation"] = "One. Two. Three. Four. Five."
    with pytest.raises(ValidationError, match="at most four sentences"):
        GeneratedMCQExam.model_validate({"items": [bad]})

@pytest.mark.parametrize(
    "mutate",
    [
        lambda q: q["options"]["A"].update(source="invented.pdf"),
        lambda q: q["options"]["A"].update(page="19"),
        lambda q: q["options"]["A"].update(page=17, page_end=19),
        lambda q: q["options"]["A"].update(page="17-x"),
    ],
    ids=[
        "unknown-source", "outside-supplied-pages", "range-extends-outside",
        "nonnumeric-range",
    ],
)
def test_invalid_option_citation_invalidates_whole_question(mutate):
    raw_item = item()
    mutate(raw_item)
    instance = generator({"questions": [raw_item]})
    chunks = [Chunk("c1", "material", "week3/notes.pdf", 17, 18)]

    if raw_item["options"]["A"]["page"] == "17-x":
        with pytest.raises(GenerationError):
            instance.mock_exam("IFN580", chunks, count=1, max_attempts=1)
    else:
        result = instance.mock_exam("IFN580", chunks, count=1, max_attempts=1)
        assert result.exam.items == []
        assert len(result.dropped) == 1


def test_normalization_never_repairs_semantic_fields():
    raw_item = item(correct_option="a")
    result = generator({"questions": [raw_item]}).mock_exam(
        "IFN580", [Chunk("c1", "material", "week3/notes.pdf", 17)],
        count=1,
        max_attempts=1,
    )
    assert result.exam.items[0].correct_option == "A"


def test_mcq_wire_rejects_unsupported_top_level_fields():
    instance = generator({"questions": [item()], "metadata": {}})
    with pytest.raises(GenerationError):
        instance.mock_exam(
            "IFN580", [Chunk("c1", "material", "week3/notes.pdf", 17)],
            count=1,
            max_attempts=1,
        )


def test_mcq_prompt_requests_exact_contract_and_no_short_answer_fields():
    raw_item = item()
    instance = generator({"questions": [raw_item]})
    instance.mock_exam(
        "IFN580", [Chunk("c1", "material", "week3/notes.pdf", 17)],
        count=1,
        max_attempts=1,
    )
    messages = instance._client.calls[0]["messages"]
    prompt = "\n".join(message["content"] for message in messages)
    assert "exactly four options" in prompt
    assert "correct_option" in prompt
    assert "page_end" in prompt
    assert "negative" in prompt.lower() or "NOT" in prompt
    assert "answer    complete" not in prompt


def test_render_mcq_hides_topics_and_citations_on_paper_but_teaches_in_key():
    raw = item()
    # The model boundary normally performs this mechanical normalization.
    raw["options"] = {label: option(label) for label in reversed("ABCD")}
    for label, value in raw["options"].items():
        value["text"] = f"Choice {label}."
    raw["correct_option"] = "B"
    exam = GeneratedMCQExam.model_validate({"items": [raw]})

    paper, answers = render_exam("IFN580", exam)

    assert "Generalisation" not in paper
    assert "week3/notes.pdf" not in paper
    assert "Feedback for" not in paper
    for label in "ABCD":
        assert f"**{label}.** Choice {label}." in paper

    assert "Generalisation" in answers
    assert "Correct answer: **B. Choice B.**" in answers
    for label in "ABCD":
        assert f"**{label}. Choice {label}.**" in answers
        assert f"Feedback for {label}." in answers
    assert answers.count("week3/notes.pdf") == 4

