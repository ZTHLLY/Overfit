"""A pre-v2 legacy short-answer run remains replayable after routing changes."""

import json
from pathlib import Path

from overfit.evaluation import judge, runner


def test_pre_v2_short_answer_recording_replays_offline(tmp_path, monkeypatch):
    bundle = json.loads(
        (Path(__file__).parent / "fixtures" / "judge_v4_frozen.json").read_text()
    )
    source = tmp_path / bundle["run_id"]
    for relative, content in bundle["files"].items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def forbidden(*args, **kwargs):
        raise AssertionError("Replay cannot access runtime settings or provider")

    monkeypatch.setattr(judge, "JudgeSettings", forbidden)
    monkeypatch.setattr(judge, "make_client", forbidden)
    result = runner.replay_judgment(source / "judgments" / bundle["judgment_id"])
    assert result["status"] == "completed"
    assert result["metrics"]["model_all_positive_count"] == 1
    assert result["metrics"]["call_starts"] == 2
