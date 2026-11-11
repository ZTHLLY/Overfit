"""The v2 CLI exposes explicit non-publishing calibration and frozen preflight."""

import pytest
from typer.testing import CliRunner

from overfit import cli
from overfit.evaluation import runner


def _result(tmp_path, *, workflow="calibration", status="prepared", usage=None):
    return {
        "status": status,
        "workflow": workflow,
        "protocol_version": "mcq-judge-v2",
        "exit_code": 0,
        "report_path": tmp_path / "report.md",
        "publication": None,
        "metrics": {
            "mode": "prepare" if status == "prepared" else "execute",
            "retained_count": 30,
            "selected_evaluated_phase_count": 0,
            "selected_phase_count": 2,
            "evaluated_phase_count": 0,
            "planned_phase_count": 30,
            "model_all_positive_count": 0,
            "human_review_pending_item_count": 0,
            "technical_error_phase_count": 0,
            "mandatory_failure_item_count": 0,
            "call_starts": 0,
            "response_count": 0,
            "retry_count": 0,
            "usage": usage or {},
        },
    }


def _invoke(*args):
    return CliRunner().invoke(cli.app, ["eval", *map(str, args)])


def test_calibration_forwards_explicit_selection_and_prints_preflight_first(tmp_path, monkeypatch):
    calls = []

    def review(run_dir, **kwargs):
        cli.typer.echo("RUNNER STARTED")
        calls.append((run_dir, kwargs))
        return _result(tmp_path)

    monkeypatch.setattr(runner, "run_mcq_judge_review", review, raising=False)
    monkeypatch.setenv("JUDGE_MAX_OUTPUT_TOKENS", "SECRET_LEGACY_LIMIT")
    monkeypatch.setenv("JUDGE_MAX_ATTEMPTS", "SECRET_LEGACY_ATTEMPTS")
    monkeypatch.setenv("JUDGE_TEMPERATURE", "SECRET_LEGACY_TEMP")
    result = _invoke("judge-review", tmp_path, "--base-judgment", tmp_path / "base", "--items", "3,5")
    assert result.exit_code == 0, result.output
    assert calls == [(tmp_path, {"base_judgment": tmp_path / "base", "items": "3,5", "execute": False})]
    assert "calibration" in result.output
    assert "no publication" in result.output
    assert "8192 -> 16384" in result.output
    assert "attempts: 2" in result.output
    assert result.output.index("8192 -> 16384") < result.output.index("RUNNER STARTED")
    for name in ("JUDGE_MAX_OUTPUT_TOKENS", "JUDGE_MAX_ATTEMPTS", "JUDGE_TEMPERATURE"):
        assert name in result.output
    assert "legacy-only" in result.output
    assert "SECRET_LEGACY" not in result.output
    assert "every required dimension" in result.output
    assert "all three dimensions" not in result.output


@pytest.mark.parametrize("options", [[], ["--items", "3,5"], ["--base-judgment", "base"]])
def test_calibration_requires_both_explicit_items_and_base(tmp_path, monkeypatch, options):
    def forbidden(*args, **kwargs):
        raise AssertionError("Incomplete CLI cannot start a runner")

    monkeypatch.setattr(runner, "run_mcq_judge_review", forbidden, raising=False)
    result = _invoke("judge-review", tmp_path, *options)
    assert result.exit_code == 2


def test_calibration_execute_is_explicit_and_unknown_policy_flags_fail(tmp_path, monkeypatch):
    calls = []

    def review(run_dir, **kwargs):
        calls.append(kwargs)
        return _result(tmp_path, status="completed")

    monkeypatch.setattr(runner, "run_mcq_judge_review", review, raising=False)
    args = ["judge-review", tmp_path, "--base-judgment", tmp_path / "base", "--items", "3,5"]
    executed = _invoke(*args, "--execute")
    assert executed.exit_code == 0, executed.output
    assert calls[0]["execute"] is True
    for flag in ("--max-attempts", "--max-output-tokens", "--temperature"):
        rejected = _invoke(*args, flag, "1")
        assert rejected.exit_code == 2
    assert len(calls) == 1


def test_summary_distinguishes_formal_and_calibration_and_only_known_usage(tmp_path):
    for workflow, expected in [("formal", "formal complete"), ("calibration", "calibration")]:
        result = _result(tmp_path, workflow=workflow, status="completed", usage={"reasoning_tokens": 0, "visible_completion_tokens": 25})
        with CliRunner().isolation() as output:
            cli._judge_summary(result)
            text = output[0].getvalue().decode()
        assert expected in text
        assert "Mandatory failures: 0" in text
        assert "reasoning_tokens=0" in text
        assert "visible_completion_tokens=25" in text
        assert "prompt_tokens=" not in text
        if workflow == "calibration":
            assert "no publication" in text


def test_calibration_runner_errors_are_safely_presented(tmp_path, monkeypatch):
    def review(*args, **kwargs):
        raise runner.JudgeRunError("Selected items must be strictly increasing.")

    monkeypatch.setattr(runner, "run_mcq_judge_review", review, raising=False)
    result = _invoke("judge-review", tmp_path, "--base-judgment", "base", "--items", "5,3")
    assert result.exit_code == 1
    assert "strictly increasing" in result.output


def test_judge_help_distinguishes_short_answer_budget_and_default_mcq_v1():
    result = _invoke("judge", "--help")
    assert result.exit_code == 0
    assert "short-answer" in result.output
    assert "MCQ Judge v1" in result.output
    assert "full" in result.output
    assert "MCQ v2 formal" not in result.output
