"""CLI challenge is opt-in, standalone, and reports case rather than generation metrics."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from overfit import cli
from overfit.evaluation import challenge, judge
from tests.test_judge_challenge import (
    Fake,
    forbidden,
    make_setup,
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    return make_setup(tmp_path, monkeypatch)


def invoke(*args):
    return CliRunner().invoke(cli.app, ["eval", "judge-challenge", *map(str, args)])


def report_path(output):
    return Path(
        next(
            line.removeprefix("Report: ")
            for line in output.splitlines()
            if line.startswith("Report: ")
        )
    )


def test_prepare_cli_does_not_access_environment_or_index(setup, monkeypatch):
    monkeypatch.setattr(judge, "JudgeSettings", forbidden)
    monkeypatch.setattr(judge, "make_client", forbidden)
    monkeypatch.setattr(cli, "get_settings", forbidden)
    monkeypatch.setattr(cli, "_open_store", forbidden)
    result = invoke(setup.suite)
    assert result.exit_code == 0, result.output
    assert (
        "challenge status: prepared" in result.output
        and "not a formal generation evaluation" in result.output
    )
    assert "Offline preparation only: no model calls" in result.output
    assert "Overall coverage: 0/14" in result.output and "Cases: 7" in result.output
    assert report_path(result.output).is_file()


def test_execute_cli_uses_existing_judge_settings_and_explicit_output(
    setup, monkeypatch
):
    fake = Fake()
    monkeypatch.setattr(judge, "JudgeSettings", lambda: setup.settings)
    monkeypatch.setattr(judge, "make_client", lambda _: fake)
    result = invoke(
        setup.suite,
        "--execute",
        "--max-calls",
        2,
        "--max-items",
        1,
        "--output-dir",
        setup.root / "custom",
    )
    assert result.exit_code == 0, result.output
    assert "status: completed" in result.output
    assert "Selected reviews completed: 2/2" in result.output and "Overall coverage: 2/14" in result.output
    assert "Responses received: 2" in result.output and len(fake.calls) == 2
    assert report_path(result.output).parent.parent == setup.root / "custom"


def test_budget_is_required_before_configuration(setup, monkeypatch):
    monkeypatch.setattr(judge, "JudgeSettings", forbidden)
    result = invoke(setup.suite, "--execute")
    assert result.exit_code == 1 and "--max-calls" in result.output
    assert not (setup.root / "outputs").exists()


@pytest.mark.parametrize(
    "args",
    [
        ("--max-calls", "-1"),
        ("--max-calls", "x"),
        ("--max-items", "0"),
        ("--max-items", "-1"),
    ],
)
def test_invalid_flags_are_rejected(setup, monkeypatch, args):
    monkeypatch.setattr(challenge, "run_challenge", forbidden)
    assert invoke(setup.suite, *args).exit_code == 2


def test_cli_incomplete_is_nonzero_with_report(setup, monkeypatch):
    fake = Fake()
    monkeypatch.setattr(judge, "JudgeSettings", lambda: setup.settings)
    monkeypatch.setattr(judge, "make_client", lambda _: fake)
    result = invoke(setup.suite, "--execute", "--max-items", 1, "--max-calls", 1)
    assert result.exit_code == 1 and "status: incomplete" in result.output
    assert "Selected reviews completed: 1/2" in result.output and report_path(result.output).is_file()


def test_cli_hides_untrusted_io_exception(setup, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("SECRET_TOKEN")

    monkeypatch.setattr(challenge, "run_challenge", fail)
    result = invoke(setup.suite)
    assert result.exit_code == 1 and "SECRET_TOKEN" not in result.output
    assert "Traceback" not in result.output


def test_challenge_help_exposes_only_supported_workflow():
    result = invoke("--help")
    assert result.exit_code == 0
    for flag in ("--execute", "--max-calls", "--max-items", "--output-dir"):
        assert flag in result.output
    assert "--resume" not in result.output


def test_english_challenge_labels_preserve_unicode_user_output_path(setup):
    result = invoke(setup.suite, "--output-dir", setup.root / "中文报告")
    assert result.exit_code == 0
    path = report_path(result.output)
    assert "中文报告" in str(path) and path.is_file()
    assert "Preliminary automated review; not yet human-calibrated." in result.output
    assert all(line.isascii() for line in result.output.splitlines()
               if not line.startswith("Report: "))
    # This change is terminal-only: the existing local report language is untouched.
    assert "人工挑战试验" in path.read_text()
