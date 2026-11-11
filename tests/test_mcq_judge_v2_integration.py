"""Real generation artifacts through the v2 writer, calibration and offline replay."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from overfit.evaluation import judge, runner
from tests import test_eval_cli as generation
from tests.test_mcq_judge_v2_runner import review, settings

setup = generation.setup


class SourceAwareClient:
    def __init__(self, *, legacy=False):
        self.legacy = legacy
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.calls.append(request)
        item = json.loads(request["messages"][1]["content"])["item"]
        value = review()
        value["other_options_incorrect"] = {
            label: label != item["correct_option"] for label in "ABCD"
        }
        if self.legacy:
            del value["options_material_derived"]
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(value), refusal=None),
                finish_reason="stop",
            )],
            usage=None, model="judge-fixture",
        )


def test_actual_generation_formal_v2_publication_replays_without_settings(setup, monkeypatch):
    generated = generation.run(setup)
    assert generated["status"] == "completed"
    source = Path(generated["run_dir"])
    before = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    client = SourceAwareClient()
    result = runner.run_mcq_judge_v2(
        source, execute=True, settings=settings(), client_factory=lambda _: client,
    )
    assert result["status"] == "completed"
    assert len(result["accepted_items"]) == 1
    assert len(client.calls) == 1
    assert result["publication"] is not None
    assert before == {name: (source / name).read_bytes() for name in before}

    from overfit.evaluation import mcq_protocols

    def forbidden(*args, **kwargs):
        pytest.fail("Replay cannot access settings, index or provider")

    monkeypatch.setattr(mcq_protocols, "MCQJudgeV2Settings", forbidden)
    monkeypatch.setattr(judge, "JudgeSettings", forbidden)
    monkeypatch.setattr(judge, "make_client", forbidden)
    replay = runner.replay_judgment(result["judgment_dir"])
    assert replay["status"] == "completed"
    assert replay["metrics"]["accepted_count"] == 1


def test_actual_generation_and_frozen_v1_base_calibrate_without_mutation(setup):
    generated = generation.run(setup)
    source = Path(generated["run_dir"])
    # The default v1 writer creates comparison evidence, not reusable v2 decisions.
    legacy_settings = judge.JudgeSettings(
        _env_file=None, base_url="https://judge.example/v1", api_key="fixture",
        model="judge-fixture",
    )
    base = runner.run_mcq_judge(
        source, execute=True, settings=legacy_settings,
        client_factory=lambda _: SourceAwareClient(legacy=True),
    )
    base_root = Path(base["judgment_dir"])
    before = {p.name: p.read_bytes() for p in base_root.iterdir() if p.is_file()}
    result = runner.run_mcq_judge_review(
        source, base_judgment=base_root, items="1", execute=True,
        settings=settings(), client_factory=lambda _: SourceAwareClient(),
    )
    assert result["status"] == "completed"
    assert result["publication"] is None
    root = Path(result["judgment_dir"])
    assert not (root / "judged_mock_exam.md").exists()
    assert not (root / "judged_answers.md").exists()
    assert "no publication" in (root / "report.md").read_text()
    assert before == {p.name: p.read_bytes() for p in base_root.iterdir() if p.is_file()}
    assert not (base_root / "replays").exists()
    assert runner.replay_judgment(root)["status"] == "completed"
