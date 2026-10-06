"""CLI prints actionable reports, defaults offline, and forwards explicit budgets."""

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from typer.testing import CliRunner

from overfit import cli
from overfit.evaluation import judge, runner
from overfit.evaluation.contracts import TraceReadError
from tests.test_eval_replay import fixture_run


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return fixture_run(tmp_path).run_dir


def invoke(*args):
    return CliRunner().invoke(cli.app, ["eval", *map(str, args)])


def report_path(output):
    return Path(next(line.removeprefix("Report: ") for line in output.splitlines()
                     if line.startswith("Report: ")))


def forbidden(*args, **kwargs):
    raise AssertionError("Preparation/replay must not load settings, services or indexes")


def test_prepare_and_replay_do_not_load_env_model_or_index(source, monkeypatch):
    (Path.cwd() / ".env").write_text("JUDGE_API_KEY=SECRET_SHOULD_NOT_LOAD\n")
    monkeypatch.setattr(cli, "get_settings", forbidden)
    monkeypatch.setattr(cli, "_open_store", forbidden)
    monkeypatch.setattr(judge, "JudgeSettings", forbidden)
    monkeypatch.setattr(judge, "make_client", forbidden)
    monkeypatch.setattr(judge, "invoke", forbidden)
    original = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    prepared = invoke("judge", source)
    assert prepared.exit_code == 0, prepared.output
    assert "Prepare only: no judge calls; semantics not evaluated" in prepared.output
    assert "Selected reviews completed: 0/2" in prepared.output
    path = report_path(prepared.output)
    assert path.is_absolute() and path.is_file()
    assert "SECRET_SHOULD_NOT_LOAD" not in path.read_text()
    replayed = invoke("judge-replay", path.parent)
    assert replayed.exit_code == 0, replayed.output
    assert "Offline replay only; no new model calls" in replayed.output
    assert report_path(replayed.output).is_file()
    assert report_path(replayed.output) != path
    assert original == {name: (source / name).read_bytes() for name in original}


def test_execute_requires_explicit_budget_before_configuration(source, monkeypatch):
    monkeypatch.setattr(judge, "JudgeSettings", forbidden)
    monkeypatch.setattr(judge, "make_client", forbidden)
    result = invoke("judge", source, "--execute")
    assert result.exit_code == 1
    assert "--max-calls" in result.output
    assert not (source / "judgments").exists()


def test_execute_missing_independent_configuration_is_safe(source, monkeypatch):
    for key in ("JUDGE_MODEL", "JUDGE_BASE_URL", "JUDGE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "SECRET_GENERATOR_KEY")
    monkeypatch.setattr(judge, "make_client", forbidden)
    result = invoke("judge", source, "--execute", "--max-calls", 2)
    assert result.exit_code == 1
    assert "JUDGE_MODEL" in result.output
    assert "SECRET_GENERATOR_KEY" not in result.output
    assert not (source / "judgments").exists()


@pytest.mark.parametrize("args", [("--max-calls", "-1"), ("--max-items", "0"),
                                  ("--max-items", "-1"), ("--max-calls", "x")])
def test_cli_rejects_invalid_budget_flags(source, args, monkeypatch):
    monkeypatch.setattr(runner, "run_judge", forbidden)
    result = invoke("judge", source, *args)
    assert result.exit_code == 2


def install_fake_judge(monkeypatch):
    settings = judge.JudgeSettings(base_url="https://fixture.invalid/v1", api_key="SECRET_JUDGE_KEY",
                                   model="test-judge", _env_file=None)
    monkeypatch.setattr(judge, "JudgeSettings", lambda: settings)
    evidence = [{"chunk_id": "c1", "quote": "Synthetic evidence"}]
    bodies = [
        {"verdict": "answerable", "reason": "Enough evidence.", "evidence": evidence},
        {"material_support": "supported", "citation_support": "supported", "reason": "Supported.",
         "claims": [{"text": "A", "verdict": "supported", "evidence": evidence}],
         "citation_evidence": evidence},
    ]
    calls = []
    def create(**request):
        calls.append(request)
        return NS(choices=[NS(message=NS(content=json.dumps(bodies[len(calls) - 1]),
                                        reasoning_content="DO_NOT_SAVE_REASONING"), finish_reason="stop")],
                  usage=NS(prompt_tokens=10, completion_tokens=5, total_tokens=15), model="test-judge")
    client = NS(chat=NS(completions=NS(create=create)))
    monkeypatch.setattr(judge, "make_client", lambda settings: client)
    return calls


def test_execute_fake_judge_and_replay_print_report_and_coverage(source, monkeypatch):
    calls = install_fake_judge(monkeypatch)
    result = invoke("judge", source, "--execute", "--max-calls", 2, "--max-items", 1)
    assert result.exit_code == 0, result.output
    assert "status: completed" in result.output
    assert "Selected reviews completed: 2/2" in result.output
    assert "Model positive on all three dimensions: 1" in result.output
    assert "Model call attempts: 2" in result.output
    path = report_path(result.output)
    assert path.is_file() and len(calls) == 2
    files = "".join(p.read_text() for p in path.parent.iterdir() if p.is_file())
    assert "SECRET_JUDGE_KEY" not in files
    assert "DO_NOT_SAVE_REASONING" not in files
    monkeypatch.setattr(judge, "JudgeSettings", forbidden)
    monkeypatch.setattr(judge, "make_client", forbidden)
    monkeypatch.setattr(judge, "invoke", forbidden)
    replay = invoke("judge-replay", path.parent)
    assert replay.exit_code == 0, replay.output
    assert "Recorded model call attempts: 2" in replay.output
    assert "Offline replay only; no new model calls" in replay.output
    assert report_path(replay.output).is_file()


def test_incomplete_execution_returns_nonzero_but_prints_report(source, monkeypatch):
    calls = install_fake_judge(monkeypatch)
    result = invoke("judge", source, "--execute", "--max-calls", 1)
    assert result.exit_code == 1, result.output
    assert "status: incomplete" in result.output
    assert "Selected reviews completed: 1/2" in result.output
    assert report_path(result.output).is_file()
    assert len(calls) == 1
    replay = invoke("judge-replay", report_path(result.output).parent)
    assert replay.exit_code == 1, replay.output
    assert "status: incomplete" in replay.output
    assert report_path(replay.output).is_file()


def test_corrupt_replay_reports_safe_error(source):
    result = invoke("judge-replay", source)
    assert result.exit_code == 1
    assert "Traceback" not in result.output


@pytest.mark.parametrize("command,target", [("judge", "run_judge"), ("judge-replay", "replay_judgment")])
def test_untrusted_exception_message_is_not_printed(source, monkeypatch, command, target):
    def fail(*args, **kwargs):
        raise TraceReadError("SECRET_API_KEY=private")
    monkeypatch.setattr(runner, target, fail)
    result = invoke(command, source)
    assert result.exit_code == 1
    assert "private" not in result.output
    assert "SECRET_API_KEY" not in result.output


def test_help_exposes_explicit_execution_flags():
    result = invoke("judge", "--help")
    assert result.exit_code == 0
    for flag in ("--execute", "--max-calls", "--max-items"):
        assert flag in result.output


def test_selected_success_prints_both_selected_and_full_coverage(tmp_path, monkeypatch):
    from tests.test_judge_runner import make_source
    monkeypatch.chdir(tmp_path)
    source = make_source(tmp_path, count=3)
    calls = install_fake_judge(monkeypatch)
    result = invoke("judge", source, "--execute", "--max-calls", 2, "--max-items", 1)
    assert result.exit_code == 0 and "status: completed" in result.output
    assert "Selected reviews completed: 2/2" in result.output
    assert "Overall coverage: 2/6" in result.output
    assert "Model positive on all three dimensions: 1 (not final approval)" in result.output
    assert "Automatically approved" not in result.output and len(calls) == 2


@pytest.mark.parametrize("function", ["_judge_summary", "eval_judge", "eval_judge_replay"])
def test_fixed_judge_terminal_text_is_english_without_restricting_user_data(function):
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(getattr(cli, function)))
    literals = [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    # Inspect only fixed application literals, never report paths or model/user content.
    assert all(not any("\u3400" <= char <= "\u9fff" for char in text) for text in literals)


def test_english_judge_and_replay_labels_preserve_unicode_report_paths(tmp_path, monkeypatch):
    directory = tmp_path / "课程"
    directory.mkdir()
    monkeypatch.chdir(directory)
    source = fixture_run(directory).run_dir
    prepared = invoke("judge", source)
    assert prepared.exit_code == 0
    path = report_path(prepared.output)
    assert "课程" in str(path) and path.is_file()
    replay = invoke("judge-replay", path.parent)
    assert replay.exit_code == 0
    for result in (prepared, replay):
        assert "Preliminary automated review; not yet human-calibrated." in result.output
        assert "课程" in str(report_path(result.output))
        assert all(line.isascii() for line in result.output.splitlines()
                   if not line.startswith("Report: "))
