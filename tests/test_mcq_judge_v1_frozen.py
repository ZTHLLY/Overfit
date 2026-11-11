"""Historical v1 bundles captured before introducing the v2 write lane."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from overfit.evaluation import judge, runner

FIXTURES = Path(__file__).parent / "fixtures" / "mcq_judge_v1"


@pytest.mark.parametrize("case", ["prepared", "completed", "incomplete"])
def test_frozen_v1_recording_replays_without_provider(tmp_path, monkeypatch, case):
    bundle = json.loads((FIXTURES / f"{case}.json").read_text())
    root = tmp_path / "judgments" / bundle["judgment_id"]
    root.mkdir(parents=True)
    for name, content in bundle["artifacts"].items():
        (root / name).write_text(content)
    monkeypatch.setattr(
        runner, "_mcq_source", lambda _: (tmp_path, {}, bundle["tasks"])
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Historical replay must not construct a provider")

    monkeypatch.setattr(judge, "make_client", forbidden)
    result = runner.replay_judgment(root)
    assert result["status"] == case
    assert result["metrics"]["call_starts"] == (0 if case == "prepared" else 1)
    if case == "incomplete":
        assert result["metrics"]["retry_count"] == 0


def test_frozen_v1_request_reconstructed_exactly():
    bundle = json.loads((FIXTURES / "completed.json").read_text())
    manifest = json.loads(bundle["artifacts"]["manifest.json"])
    recorded = json.loads(bundle["artifacts"]["calls.jsonl"].splitlines()[0])["request"]
    tasks = bundle["tasks"]
    actual = judge.build_mcq_request(
        tasks["items"][0], tasks["item_materials"][0],
        SimpleNamespace(**manifest["judge_config"]),
    )
    assert runner.canonical(actual) == runner.canonical(recorded)
    assert set(manifest) == {
        "schema_version", "judgment_id", "mode", "created_at", "protocol_version",
        "source_identity", "judge_config", "max_calls", "max_items", "generation_contract",
    }
    assert actual["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("finish,expected_status", [("stop", "completed"), ("length", "incomplete")])
def test_default_mcq_writer_restores_v1_request_budget_and_terminal_length(
    tmp_path, monkeypatch, finish, expected_status
):
    bundle = json.loads((FIXTURES / "completed.json").read_text())
    tasks = bundle["tasks"]
    raw_content = json.loads(bundle["artifacts"]["calls.jsonl"].splitlines()[1])["response"]["content"]
    monkeypatch.setattr(runner, "_mcq_source", lambda _: (tmp_path, {}, tasks))
    monkeypatch.setattr(runner, "inspect_run", lambda _: {"generation_contract": "mcq-v1"})
    calls = []

    def create(**request):
        calls.append(request)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=raw_content if finish == "stop" else "", refusal=None),
                finish_reason=finish,
            )],
            usage=None, model="judge-fixture",
        )

    config = judge.JudgeSettings(
        _env_file=None, base_url="https://judge.example/v1", api_key="fixture",
        model="judge-fixture", max_attempts=3, max_output_tokens=4321, temperature=0.25,
    )
    result = runner.run_judge(
        tmp_path, execute=True, settings=config,
        client_factory=lambda _: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    root = Path(result["judgment_dir"])
    manifest = json.loads((root / "manifest.json").read_text())
    assert result["protocol_version"] == manifest["protocol_version"] == "mcq-judge-v1"
    assert "protocol_digest" not in manifest
    assert result["status"] == expected_status
    assert result["metrics"]["max_calls"] == 3
    assert len(calls) == 1
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["max_tokens"] == 4321 and calls[0]["temperature"] == 0.25
    assert "max_completion_tokens" not in calls[0] and "extra_body" not in calls[0]
    assert (result["publication"] is not None) == (expected_status == "completed")
    replay = runner.replay_judgment(root)
    assert replay["status"] == expected_status and replay["protocol_version"] == "mcq-judge-v1"


def test_default_mcq_prepare_and_budget_guards_remain_offline(tmp_path, monkeypatch):
    from tests.test_mcq_judge import _source

    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path, item_count=2))
    monkeypatch.setattr(runner, "inspect_run", lambda _: {"generation_contract": "mcq-v1"})

    def forbidden(*_args, **_kwargs):
        pytest.fail("No Judge settings or client should be loaded")

    monkeypatch.setattr(judge, "JudgeSettings", forbidden)
    monkeypatch.setattr(judge, "make_client", forbidden)
    with pytest.raises(runner.JudgeRunError, match="--max-calls does not apply"):
        runner.run_judge(tmp_path, execute=True, max_calls=1)
    with pytest.raises(runner.JudgeRunError, match="every retained item"):
        runner.run_judge(tmp_path, execute=True, max_items=1)
    prepared = runner.run_mcq_judge(tmp_path, max_items=1)
    assert prepared["protocol_version"] == "mcq-judge-v1" and prepared["status"] == "prepared"
    assert prepared["metrics"]["selected_phase_count"] == 1
    assert prepared["metrics"]["call_starts"] == prepared["metrics"]["max_calls"] == 0
    assert runner.replay_judgment(prepared["judgment_dir"])["status"] == "prepared"
