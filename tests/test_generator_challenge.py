"""Offline contracts for controlled Generator challenge fixtures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from overfit import cli
from overfit.config import LLMSettings
from overfit.evaluation.generator_challenge import (
    GeneratorChallengeError,
    load_suite,
    run_generator_challenge,
)
from overfit.evaluation.replay import replay_run
from overfit.evaluation.runner import replay_judgment, run_judge
from overfit.query.generator import Generator

LEGACY_SUITE = Path(__file__).resolve().parents[1] / "eval/generator_challenges/ifn580_v1"
MCQ_SUITE = Path(__file__).resolve().parents[1] / "eval/generator_challenges/ifn580_mcq_v1"


def _suite(root: Path, *, sentinel: str = "ANNOTATION_SECRET") -> Path:
    root.mkdir()
    inputs = {
        "schema": "overfit.generator_challenge_inputs.mcq.v1",
        "suite_id": "fixture-v1",
        "generation_contract": "mcq-v1",
        "chunks": [
            {
                "id": "c1",
                "text": "Trusted material states the answer.",
                "source": "notes.pdf",
                "page": 5,
                "page_end": None,
                "section": None,
                "provenance": None,
            }
        ],
        "cases": [
            {
                "case_id": "case-001",
                "course": "COURSE",
                "topic": "Trusted topic",
                "questions": 1,
                "chunk_ids": ["c1"],
            }
        ],
    }
    annotations = {
        "schema": "overfit.generator_challenge_annotations.mcq.v1",
        "suite_id": "fixture-v1",
        "generation_contract": "mcq-v1",
        "annotation_status": "human_designed_not_model_results",
        "model_input_policy": "Never send annotations to the model.",
        "cases": [
            {
                "case_id": "case-001",
                "case_type": "gold-hard-negative",
                "trusted_chunk_ids": ["c1"],
                "distractor_chunk_ids": [],
                "design_reason": sentinel,
                "expected_behaviors": ["Use trusted material."],
                "unacceptable_behaviors": ["Invent unsupported facts."],
            }
        ],
    }
    (root / "inputs.json").write_text(json.dumps(inputs), encoding="utf-8")
    (root / "annotations.json").write_text(
        json.dumps(annotations), encoding="utf-8"
    )
    return root


def _settings(tmp_path):
    return SimpleNamespace(
        outputs_dir=tmp_path / "outputs",
        llm=LLMSettings(_env_file=None, model="fixture-model", api_key="fixture-key"),
        temperature=0.2,
        request_timeout=30.0,
        output_path=lambda course, artifact: tmp_path / f"{course}_{artifact}",
    )


class _Client:
    def __init__(self):
        self.calls = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create)
        )

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = json.dumps(
            {
                "questions": [
                    {
                        "topic": "Trusted topic",
                        "question": "Which statement is supported?",
                        "options": {
                            label: {
                                "text": f"Option {label}",
                                "explanation": f"Feedback {label}.",
                                "source": "notes.pdf",
                                "page": 5,
                                "page_end": None,
                            }
                            for label in "ABCD"
                        },
                        "correct_option": "A",
                    }
                ]
            }
        )
        part = SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=content))],
            usage=SimpleNamespace(
                prompt_tokens=2, completion_tokens=3, total_tokens=5
            ),
        )
        return iter([part])


def _factory(client):
    def make(settings):
        result = Generator(settings)
        result._client = client
        return result

    return make


def test_versioned_real_suites_preserve_legacy_and_add_mcq_contract():
    _, legacy, _, _ = load_suite(LEGACY_SUITE)
    _, mcq, _, _ = load_suite(MCQ_SUITE)

    assert legacy["schema"] == "overfit.generator_challenge_inputs.v1"
    assert legacy["generation_contract"] == "legacy-short-answer"
    assert mcq["schema"] == "overfit.generator_challenge_inputs.mcq.v1"
    assert mcq["generation_contract"] == "mcq-v1"
    assert [case["case_id"] for case in legacy["cases"]] == [
        case["case_id"] for case in mcq["cases"]
    ]


def test_legacy_suite_prepare_is_readable_but_execution_is_rejected(tmp_path):
    prepared = run_generator_challenge(
        LEGACY_SUITE, output_dir=tmp_path / "prepare"
    )
    assert prepared["status"] == "prepared"
    manifest = json.loads((prepared["run_dir"] / "manifest.json").read_text())
    assert manifest["generation_contract"] == "legacy-short-answer"

    with pytest.raises(
        GeneratorChallengeError, match="legacy short-answer.*read-only"
    ):
        run_generator_challenge(
            LEGACY_SUITE,
            execute=True,
            max_calls=1,
            settings=_settings(tmp_path),
            trace_runner=lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("legacy suite must not execute")
            ),
        )


def test_prepare_validates_and_records_without_settings_or_trace_calls(tmp_path):
    called = False

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("prepare must not execute a trace")

    result = run_generator_challenge(
        _suite(tmp_path / "suite"),
        output_dir=tmp_path / "out",
        trace_runner=forbidden,
    )

    assert result["status"] == "prepared" and result["call_count"] == 0
    assert called is False
    manifest = json.loads((result["run_dir"] / "manifest.json").read_text())
    assert manifest["generator_config"] is None
    complete = json.loads((result["run_dir"] / "complete.json").read_text())
    assert complete["status"] == "prepared"
    assert all(
        hashlib.sha256((result["run_dir"] / name).read_bytes()).hexdigest()
        == digest
        for name, digest in complete["artifacts"].items()
    )


def test_cli_prepare_never_loads_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    suite = _suite(tmp_path / "suite")
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: (_ for _ in ()).throw(AssertionError("settings must stay cold")),
    )

    result = CliRunner().invoke(cli.app, ["eval", "generator-challenge", str(suite)])

    assert result.exit_code == 0, result.output
    assert "no settings, keys, or model calls" in result.output


def test_execute_writes_replayable_trace_and_hides_annotations(tmp_path, monkeypatch):
    # Generator reads only these non-secret runtime scalars during the traced request.
    import overfit.query.generator as generator_module

    settings = _settings(tmp_path)
    monkeypatch.setattr(
        generator_module,
        "get_settings",
        lambda: SimpleNamespace(
            temperature=settings.temperature,
            request_timeout=settings.request_timeout,
            max_retries=1,
            output_path=settings.output_path,
        ),
    )
    client = _Client()
    result = run_generator_challenge(
        _suite(tmp_path / "suite"),
        execute=True,
        max_calls=2,
        settings=settings,
        generator_factory=_factory(client),
    )

    assert result["status"] == "completed" and result["call_count"] == 1
    trace = Path(result["records"][0]["run_dir"])
    assert replay_run(trace)["trace_integrity"] == "complete"
    prepared = run_judge(trace, execute=False)
    assert prepared["status"] == "prepared"
    assert replay_judgment(prepared["judgment_dir"])["status"] == "prepared"
    request = client.calls[0]
    assert "ANNOTATION_SECRET" not in json.dumps(request)
    assert "Trusted material states the answer." in json.dumps(request)
    assert "ANNOTATION_SECRET" not in "".join(
        path.read_text(errors="replace")
        for path in trace.rglob("*")
        if path.is_file()
    )
    assert "fixture-key" not in "".join(
        path.read_text(errors="replace")
        for path in settings.outputs_dir.rglob("*")
        if path.is_file()
    )


def test_global_budget_stops_parent_after_failed_case(tmp_path):
    calls = []

    def exhausted(*args, **kwargs):
        calls.append(kwargs["max_calls"])
        return {
            "run_dir": "/tmp/trace",
            "run_id": "trace",
            "status": "failed",
            "exit_code": 1,
            "error": {"type": "operation_error", "message": "Operation failed."},
            "call_count": kwargs["max_calls"],
            "summary": None,
        }

    result = run_generator_challenge(
        _suite(tmp_path / "suite"),
        execute=True,
        max_calls=2,
        settings=_settings(tmp_path),
        trace_runner=exhausted,
    )

    assert calls == [2]
    assert result["status"] == "incomplete"
    assert result["call_count"] == 2 and result["exit_code"] == 1


def test_loader_rejects_duplicate_json_keys(tmp_path):
    suite = _suite(tmp_path / "suite")
    inputs = (suite / "inputs.json").read_text()
    (suite / "inputs.json").write_text(
        inputs.replace(
            '"schema": "overfit.generator_challenge_inputs.mcq.v1",',
            '"schema": "overfit.generator_challenge_inputs.mcq.v1", '
            '"schema": "overfit.generator_challenge_inputs.mcq.v1",',
            1,
        )
    )

    with pytest.raises(GeneratorChallengeError, match="Invalid generator"):
        load_suite(suite)


def test_loader_rejects_input_symlink(tmp_path):
    suite = _suite(tmp_path / "suite")
    annotation = suite / "annotations.json"
    target = tmp_path / "annotations-target.json"
    annotation.replace(target)
    annotation.symlink_to(target)

    with pytest.raises(GeneratorChallengeError, match="symlinks"):
        load_suite(suite)


def test_loader_rejects_page_end_before_page(tmp_path):
    suite = _suite(tmp_path / "suite")
    inputs = json.loads((suite / "inputs.json").read_text())
    inputs["chunks"][0]["page_end"] = 4
    (suite / "inputs.json").write_text(json.dumps(inputs))

    with pytest.raises(GeneratorChallengeError):
        load_suite(suite)


def test_max_items_does_not_skip_full_suite_validation(tmp_path):
    suite = _suite(tmp_path / "suite")
    inputs = json.loads((suite / "inputs.json").read_text())
    inputs["cases"].append(
        {
            "case_id": "case-invalid-later",
            "course": "COURSE",
            "topic": "Later invalid case",
            "questions": 1,
            "chunk_ids": ["missing"],
        }
    )
    annotations = json.loads((suite / "annotations.json").read_text())
    annotations["cases"].append(
        {
            "case_id": "case-invalid-later",
            "case_type": "unrelated-only",
            "trusted_chunk_ids": [],
            "distractor_chunk_ids": [],
            "design_reason": "This later case must still be validated.",
            "expected_behaviors": ["Fail validation."],
            "unacceptable_behaviors": ["Start execution."],
        }
    )
    (suite / "inputs.json").write_text(json.dumps(inputs))
    (suite / "annotations.json").write_text(json.dumps(annotations))

    with pytest.raises(GeneratorChallengeError):
        run_generator_challenge(
            suite,
            max_items=1,
            output_dir=tmp_path / "outputs",
        )
