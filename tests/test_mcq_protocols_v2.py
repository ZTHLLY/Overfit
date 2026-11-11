"""Frozen MCQ protocol behavior at the public request/review/config boundaries."""

import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from overfit.evaluation import judge
from overfit.evaluation import mcq_protocols as protocols


def settings(**overrides):
    return protocols.MCQJudgeV2Settings(
        _env_file=None, base_url="https://judge.example/v1", api_key="fixture",
        model="judge-fixture", **overrides,
    )


def review():
    return {
        "human_review_required": False, "answerable": True, "one_best_answer": True,
        "correct_option_supported": True,
        "other_options_incorrect": {"A": False, "B": True, "C": True, "D": True},
        "explanations_accurate": dict.fromkeys("ABCD", True),
        "citations_supported": dict.fromkeys("ABCD", True),
        "options_material_derived": dict.fromkeys("ABCD", True),
        "positive_stem": True, "no_calculation": True, "reason": "Fixture.",
    }


def test_v2_request_is_strict_and_uses_fixed_attempt_schedule():
    from tests.test_mcq_judge import _item

    item = _item()
    chunk = {"id": "c1", "text": "Source", "source": "notes.pdf", "page": 1, "page_end": None}
    first = protocols.build_mcq_request(item, [chunk], settings(), protocol_version="mcq-judge-v2")
    second = protocols.build_mcq_request(item, [chunk], settings(), protocol_version="mcq-judge-v2", attempt=2)
    assert first["max_completion_tokens"] == 8192
    assert second["max_completion_tokens"] == 16384
    assert first["extra_body"] == {"reasoning": {"enabled": False}, "provider": {"require_parameters": True}}
    assert first["temperature"] == 0 and first["stream"] is False
    assert "max_tokens" not in first
    envelope = first["response_format"]
    assert envelope["type"] == "json_schema" and envelope["json_schema"]["strict"] is True
    schema = envelope["json_schema"]["schema"]
    assert set(schema["required"]) == set(review())
    for key in ("options_material_derived", "citations_supported", "other_options_incorrect", "explanations_accurate"):
        option_schema = schema["properties"][key]
        assert set(option_schema["required"]) == set("ABCD")
        assert option_schema["additionalProperties"] is False


def test_citation_support_and_material_derivation_are_independent_mandatory_dimensions():
    value = review()
    value["options_material_derived"]["B"] = False
    parsed = protocols.validate_mcq_review(json.dumps(value), {"correct_option": "A"}, protocol_version="mcq-judge-v2")
    assert parsed["mandatory_pass"] is False
    assert parsed["model_review"]["citations_supported"]["B"] is True


@pytest.mark.parametrize("field", list(review()))
def test_v2_requires_every_field(field):
    value = review()
    del value[field]
    with pytest.raises(judge.JudgeOutputError):
        protocols.validate_mcq_review(json.dumps(value), {"correct_option": "A"}, protocol_version="mcq-judge-v2")


@pytest.mark.parametrize("override", [{"timeout": True}, {"timeout": "60"}, {"max_input_chars": "1000"}, {"max_input_chars": True}, {"max_attempts": 2}, {"temperature": 0}, {"extra_policy": True}])
def test_v2_rejects_coercive_or_unknown_explicit_configuration(override):
    with pytest.raises(ValidationError):
        settings(**override)


def test_legacy_environment_tuning_is_ignored(monkeypatch):
    for key in ("JUDGE_MAX_ATTEMPTS", "JUDGE_MAX_OUTPUT_TOKENS", "JUDGE_TEMPERATURE"):
        monkeypatch.setenv(key, "not-a-number")
    assert settings().max_input_chars == 80000
    assert set(protocols.v2_safe_identity(settings())) == {"model", "base_url", "timeout", "max_input_chars"}


@pytest.mark.parametrize("result,retryable,expected", [("length", False, "retry"), ("schema_error", False, "retry"), ("transport_error", True, "retry"), ("transport_error", False, "transport_error"), ("valid", False, "success"), ("refusal", False, "refusal"), ("interrupted", False, "interrupted")])
def test_attempt_disposition(result, retryable, expected):
    assert protocols.attempt_disposition("mcq-judge-v2", attempt=1, result=result, retryable=retryable) == expected
    exhausted = protocols.attempt_disposition("mcq-judge-v2", attempt=2, result=result, retryable=retryable)
    assert exhausted != "retry"
    assert protocols.attempt_disposition("mcq-judge-v1", attempt=1, result="length", max_attempts=2) == "truncated_response"


def test_usage_allowlist_and_reasoning_derivation():
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}", refusal=None, reasoning="secret reasoning"), finish_reason="stop")],
        model="fixture",
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=30, total_tokens=40, private="secret", completion_tokens_details=SimpleNamespace(reasoning_tokens=20)),
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response)))
    result = protocols.invoke_mcq_v2(client, {})
    assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 30, "total_tokens": 40, "reasoning_tokens": 20, "visible_completion_tokens": 10}
    assert "secret" not in json.dumps(result)


def test_unknown_protocol_and_invalid_attempt_fail_closed():
    with pytest.raises(ValueError):
        protocols.mcq_protocol_digest("unknown")
    for attempt in (0, 3, True, 1.0):
        with pytest.raises(ValueError):
            protocols.build_mcq_request({}, [], settings(), protocol_version="mcq-judge-v2", attempt=attempt)


def test_v1_adapter_is_byte_identical_and_legacy_digest_unchanged():
    legacy = judge.JudgeSettings(_env_file=None, base_url="https://judge.example/v1", api_key="fixture", model="fixture")
    item = {"correct_option": "A"}
    expected = judge.build_mcq_request(item, [], legacy)
    assert protocols.build_mcq_request(item, [], legacy) == expected
    assert judge.protocol_digest() == "9002cb1ca64c48a2a43ccaa26101c55348a279d37371c09d757850ef33b53d92"
    assert len(protocols.mcq_protocol_digest("mcq-judge-v2")) == 64
    modified = copy.deepcopy(expected)
    modified["messages"][0]["content"] += "changed"
    assert modified != protocols.build_mcq_request(item, [], legacy)


@pytest.mark.parametrize("change", [
    lambda value: value.update(extra=True),
    lambda value: value["options_material_derived"].update(E=True),
    lambda value: value["options_material_derived"].pop("D"),
    lambda value: value["options_material_derived"].update(A=1),
    lambda value: value.update(answerable="true"),
    lambda value: value.update(reason="   "),
])
def test_v2_rejects_extra_keys_missing_option_and_coercive_values(change):
    value = review()
    change(value)
    with pytest.raises(judge.JudgeOutputError):
        protocols.validate_mcq_review(json.dumps(value), {"correct_option": "A"}, protocol_version="mcq-judge-v2")


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens", "total_tokens"])
@pytest.mark.parametrize("bad", [True, "10", -1, float("nan"), float("inf")])
def test_usage_excludes_unsafe_numeric_values(field, bad):
    assert protocols.normalize_usage({field: bad, "provider_private_metadata": "not retained"}) is None


def test_usage_does_not_invent_visible_tokens_without_reasoning_details():
    assert protocols.normalize_usage({"completion_tokens": 30}) == {"completion_tokens": 30}
    assert protocols.normalize_usage({"completion_tokens": 10, "completion_tokens_details": {"reasoning_tokens": 20}}) == {"completion_tokens": 10, "reasoning_tokens": 20}


def test_environment_shared_numbers_are_parsed_only_at_environment_boundary(monkeypatch):
    monkeypatch.setenv("JUDGE_TIMEOUT", "12.5")
    monkeypatch.setenv("JUDGE_MAX_INPUT_CHARS", "2000")
    value = settings()
    assert value.timeout == 12.5 and value.max_input_chars == 2000
    monkeypatch.setenv("JUDGE_MAX_INPUT_CHARS", "true")
    with pytest.raises(ValidationError):
        settings()


def test_protocol_registry_is_immutable_and_binds_every_request_policy_component():
    from dataclasses import replace

    original = protocols.PROTOCOL_REGISTRY["mcq-judge-v2"]
    policy = original.policy()
    for component in ("messages", "serialization", "user_payload", "response_format", "request", "retry", "usage"):
        altered = copy.deepcopy(policy)
        altered[component] = {"changed": True}
        assert replace(original, policy_json=json.dumps(altered, sort_keys=True)).digest != original.digest
    with pytest.raises(TypeError):
        protocols.PROTOCOL_REGISTRY["unknown"] = original
    # Returned policy and request dictionaries cannot mutate the registry.
    policy["request"]["temperature"] = 1
    assert original.policy()["request"]["temperature"] == 0


def test_v2_rejects_duplicate_json_keys_and_nonfinite_values_even_in_unknown_fields():
    body = json.dumps(review())
    for malformed in (body[:-1] + ', "answerable": false}', body[:-1] + ', "ignored": NaN}'):
        with pytest.raises(judge.JudgeOutputError):
            protocols.validate_mcq_review(malformed, {"correct_option": "A"}, protocol_version="mcq-judge-v2")


@pytest.mark.parametrize("field", ["answerable", "one_best_answer", "correct_option_supported", "positive_stem", "no_calculation"])
def test_each_scalar_mandatory_dimension_can_block_acceptance(field):
    value = review()
    value[field] = False
    parsed = protocols.validate_mcq_review(json.dumps(value), {"correct_option": "A"}, protocol_version="mcq-judge-v2")
    assert parsed["mandatory_pass"] is False


@pytest.mark.parametrize("field", ["explanations_accurate", "citations_supported", "options_material_derived", "other_options_incorrect"])
@pytest.mark.parametrize("label", list("ABCD"))
def test_each_option_dimension_can_block_acceptance(field, label):
    value = review()
    value[field][label] = not value[field][label]
    parsed = protocols.validate_mcq_review(json.dumps(value), {"correct_option": "A"}, protocol_version="mcq-judge-v2")
    assert parsed["mandatory_pass"] is False


def test_human_review_is_pending_even_with_every_mandatory_dimension_positive():
    value = review()
    value["human_review_required"] = True
    parsed = protocols.validate_mcq_review(json.dumps(value), {"correct_option": "A"}, protocol_version="mcq-judge-v2")
    assert parsed["mandatory_pass"] is True
    assert parsed["human_review"] == "pending"


def test_v2_classifier_uses_registered_ceiling_not_legacy_argument_or_public_constant(monkeypatch):
    monkeypatch.setattr(protocols, "MCQ_V2_MAX_ATTEMPTS", 99)
    assert protocols.attempt_disposition("mcq-judge-v2", attempt=2, result="length", max_attempts=99) == "truncated_response"


@pytest.mark.parametrize("identity", [
    {"base_url": "https://name:password@example.com/v1"},
    {"base_url": "https://example.com/v1?api_key=secret"},
    {"base_url": "file:///private/path"},
    {"base_url": " https://example.com/v1"},
    {"model": "   "},
    {"api_key": "  "},
])
def test_v2_rejects_unsafe_shared_runtime_identity(identity):
    values = {"base_url": "https://judge.example/v1", "api_key": "fixture", "model": "fixture", **identity}
    with pytest.raises(ValidationError):
        protocols.MCQJudgeV2Settings(_env_file=None, **values)


def test_finite_integer_usage_does_not_require_lossy_float_conversion():
    large = 10 ** 400
    assert protocols.normalize_usage({"prompt_tokens": large}) == {"prompt_tokens": large}


def test_digest_freezes_the_complete_item_and_material_wire_shapes():
    policy = protocols.PROTOCOL_REGISTRY["mcq-judge-v2"].policy()
    item_shape = policy["user_payload"]["item"]
    assert set(item_shape["required"]) == {"topic", "question", "options", "correct_option"}
    options = item_shape["properties"]["options"]
    assert options["required"] == ["A", "B", "C", "D"]
    for option in options["properties"].values():
        assert set(option["required"]) == {"text", "explanation", "source", "page", "page_end"}
        assert option["additionalProperties"] is False
    material_shape = policy["user_payload"]["material_shape"]
    assert set(material_shape["required"]) == {"id", "text", "source", "page", "page_end", "section"}


def test_request_serialization_uses_the_registered_policy(monkeypatch):
    from dataclasses import replace
    from types import MappingProxyType

    from tests.test_mcq_judge import _item

    original = protocols.PROTOCOL_REGISTRY["mcq-judge-v2"]
    policy = original.policy()
    policy["serialization"]["separators"] = [",", ":"]
    policy["serialization"]["ensure_ascii"] = True
    changed = replace(original, policy_json=json.dumps(policy))
    monkeypatch.setattr(protocols, "PROTOCOL_REGISTRY", MappingProxyType({**protocols.PROTOCOL_REGISTRY, "mcq-judge-v2": changed}))
    item = _item()
    item["topic"] = "课程"
    actual = protocols.build_mcq_request(item, [], settings(), protocol_version="mcq-judge-v2")
    expected = json.dumps({"generation_contract": "mcq-v1", "item": item, "materials": []}, ensure_ascii=True, sort_keys=True, allow_nan=False, separators=(",", ":"))
    assert actual["messages"][1]["content"] == expected
    assert changed.digest != original.digest


def test_literal_v2_wire_snapshot_survives_adding_a_later_protocol(monkeypatch):
    from dataclasses import replace
    from pathlib import Path
    from types import MappingProxyType

    fixture = json.loads((Path(__file__).parent / "fixtures" / "mcq_judge_v2_request.json").read_text())
    original = protocols.PROTOCOL_REGISTRY["mcq-judge-v2"]
    future_policy = original.policy()
    future_policy["protocol_version"] = "mcq-judge-v3"
    future_policy["messages"]["system"] = "A distinct future protocol."
    future_policy["serialization"]["separators"] = [",", ":"]
    future = replace(original, version="mcq-judge-v3", policy_json=json.dumps(future_policy))
    monkeypatch.setattr(protocols, "PROTOCOL_REGISTRY", MappingProxyType({**protocols.PROTOCOL_REGISTRY, "mcq-judge-v3": future}))
    monkeypatch.setattr(protocols, "MCQ_CURRENT_PROTOCOL_VERSION", "mcq-judge-v3")
    monkeypatch.setattr(protocols, "_V2_SYSTEM", "A mutable module name cannot rewrite frozen v2.")
    monkeypatch.setattr(protocols, "_V2_ITEM_SHAPE", {})
    value = protocols.MCQJudgeV2Settings(_env_file=None, api_key="fixture", **fixture["runtime"])
    assert protocols.mcq_protocol_digest("mcq-judge-v2") == fixture["protocol_digest"]
    for attempt in (1, 2):
        request = protocols.build_mcq_request(fixture["item"], fixture["chunks"], value, protocol_version="mcq-judge-v2", attempt=attempt)
        assert json.dumps(request, ensure_ascii=False, sort_keys=True, allow_nan=False) == fixture["canonical_requests"][str(attempt)]


@pytest.mark.parametrize("location", ["item", "option"])
def test_new_generator_fields_cannot_silently_expand_the_v2_wire_shape(location):
    from tests.test_mcq_judge import _item

    item = _item()
    if location == "item":
        item["future_generator_field"] = "unsupported"
    else:
        item["options"]["B"]["future_generator_field"] = "unsupported"
    with pytest.raises(ValueError, match="frozen protocol wire shape"):
        protocols.build_mcq_request(item, [], settings(), protocol_version="mcq-judge-v2")
