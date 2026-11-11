"""Public runner/replay contracts for the immutable MCQ Judge v2 lane."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from overfit.evaluation import runner
from tests.test_mcq_judge import _rehash_complete, _source


def review(*, pending=False, derived=True):
    return {
        "human_review_required": pending, "answerable": True,
        "one_best_answer": True, "correct_option_supported": True,
        "other_options_incorrect": {label: label != "A" for label in "ABCD"},
        "explanations_accurate": dict.fromkeys("ABCD", True),
        "citations_supported": dict.fromkeys("ABCD", True),
        "options_material_derived": dict.fromkeys("ABCD", derived),
        "positive_stem": True, "no_calculation": True, "reason": "Fixture review.",
    }


class Client:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.calls.append(request)
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        content, finish = result if isinstance(result, tuple) else (json.dumps(result), "stop")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content, refusal=None),
                                     finish_reason=finish)],
            usage=None, model="judge-fixture",
        )


def settings():
    from overfit.evaluation.mcq_protocols import MCQJudgeV2Settings
    return MCQJudgeV2Settings(
        _env_file=None, base_url="https://judge.example/v1", api_key="fixture",
        model="judge-fixture",
    )


def test_new_writes_use_v2_and_recover_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path))
    client = Client([("", "length"), review()])
    result = runner.run_mcq_judge_v2(tmp_path, execute=True, settings=settings(),
                                  client_factory=lambda _: client)
    root = Path(result["judgment_dir"])
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["protocol_version"] == "mcq-judge-v2"
    assert len(manifest["protocol_digest"]) == 64
    assert [call["max_completion_tokens"] for call in client.calls] == [8192, 16384]
    assert result["status"] == "completed"
    assert result["publication"] is not None
    assert runner.replay_judgment(root)["metrics"]["accepted_count"] == 1


def test_prepared_replay_never_loads_settings_or_constructs_client(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path))
    result = runner.run_mcq_judge_v2(tmp_path)
    from overfit.evaluation import mcq_protocols
    monkeypatch.setattr(mcq_protocols, "MCQJudgeV2Settings", lambda **_: pytest.fail("settings read"))
    monkeypatch.setattr(runner.judge, "make_client", lambda _: pytest.fail("client built"))
    replay = runner.replay_judgment(result["judgment_dir"])
    assert replay["status"] == "prepared"
    assert replay["metrics"]["max_calls"] == 2


def test_missing_required_retry_is_rejected_even_with_matching_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path))
    result = runner.run_mcq_judge_v2(tmp_path, execute=True, settings=settings(),
                                  client_factory=lambda _: Client([("", "length"), review()]))
    root = Path(result["judgment_dir"])
    events = (root / "calls.jsonl").read_text().splitlines()
    (root / "calls.jsonl").write_text("\n".join(events[:2]) + "\n")
    _rehash_complete(root, "calls.jsonl")
    with pytest.raises(runner.JudgeRunError):
        runner.replay_judgment(root)


@pytest.mark.parametrize("items", ["", "1,1", "2,1", "0", "-1", "1.0", "true", "3", " 1", "1,", [], [True], [1.0]])
def test_invalid_selection_fails_before_settings(tmp_path, monkeypatch, items):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path, item_count=2))
    with pytest.raises(runner.JudgeRunError):
        runner.run_mcq_judge_review(tmp_path, base_judgment=tmp_path / "absent", items=items,
                                    execute=True, client_factory=lambda _: pytest.fail("client built"))


@pytest.mark.parametrize("results,calls,status", [
    ([("", "length"), ("", "length")], 2, "incomplete"),
    ([("{}", "stop"), review()], 2, "completed"),
    ([TimeoutError(), review()], 2, "completed"),
    ([ConnectionError(), ConnectionError()], 2, "incomplete"),
    ([RuntimeError("private details")], 1, "incomplete"),
    ([("", "refusal")], 1, "incomplete"),
    ([("", None)], 1, "incomplete"),
    ([("", "tool_calls")], 1, "incomplete"),
    ([("", "content_filter")], 1, "incomplete"),
    ([KeyboardInterrupt()], 1, "interrupted"),
])
def test_finite_retry_state_machine_replays(tmp_path, monkeypatch, results, calls, status):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path))
    client = Client(results)
    result = runner.run_mcq_judge_v2(tmp_path, execute=True, settings=settings(),
                                  client_factory=lambda _: client)
    assert len(client.calls) == calls
    assert result["status"] == status
    assert (result["publication"] is not None) is (status == "completed")
    assert runner.replay_judgment(result["judgment_dir"])["status"] == status
    assert "private details" not in (Path(result["judgment_dir"]) / "calls.jsonl").read_text()


def base_bundle(tmp_path, monkeypatch):
    bundle = json.loads((Path(__file__).parent / "fixtures" / "mcq_judge_v1" / "completed.json").read_text())
    base = tmp_path / "judgments" / bundle["judgment_id"]
    base.mkdir(parents=True)
    for name, text in bundle["artifacts"].items():
        (base / name).write_text(text)
    monkeypatch.setattr(runner, "_mcq_source", lambda _: (tmp_path, {}, bundle["tasks"]))
    return base


def test_calibration_is_append_only_nonpublishing_and_replays_without_settings(tmp_path, monkeypatch):
    base = base_bundle(tmp_path, monkeypatch)
    before = {p.name: p.read_bytes() for p in base.iterdir()}
    first = runner.run_mcq_judge_review(tmp_path, base_judgment=base, items="1", execute=True,
                                        settings=settings(), client_factory=lambda _: Client([review()]))
    second = runner.run_mcq_judge_review(tmp_path, base_judgment=base, items=[1])
    assert first["judgment_dir"] != second["judgment_dir"]
    assert first["publication"] is None and first["accepted_items"] == []
    assert first["metrics"]["accepted_count"] == 0
    root = Path(first["judgment_dir"])
    assert "no publication" in (root / "report.md").read_text()
    assert "no publication" in (root / "manifest.json").read_text()
    assert not (root / "judged_answers.md").exists()
    assert {p.name: p.read_bytes() for p in base.iterdir()} == before
    monkeypatch.setattr(runner.judge, "JudgeSettings", lambda **_: pytest.fail("legacy settings read"))
    from overfit.evaluation import mcq_protocols
    monkeypatch.setattr(mcq_protocols, "MCQJudgeV2Settings", lambda **_: pytest.fail("v2 settings read"))
    assert runner.replay_judgment(root)["status"] == "completed"
    assert runner.replay_judgment(second["judgment_dir"])["status"] == "prepared"


def test_calibration_replay_rejects_base_mutation(tmp_path, monkeypatch):
    base = base_bundle(tmp_path, monkeypatch)
    result = runner.run_mcq_judge_review(tmp_path, base_judgment=base, items="1")
    (base / "report.md").write_text("changed")
    with pytest.raises(runner.JudgeRunError):
        runner.replay_judgment(result["judgment_dir"])


def test_public_replay_rejects_symlink_root_and_ancestor(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path))
    result = runner.run_mcq_judge_v2(tmp_path)
    root = Path(result["judgment_dir"])
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(runner.JudgeRunError):
        runner.replay_judgment(alias)
    ancestor = tmp_path / "ancestor"
    ancestor.symlink_to(root.parent, target_is_directory=True)
    with pytest.raises(runner.JudgeRunError):
        runner.replay_judgment(ancestor / root.name)


def test_derived_visible_usage_cannot_be_removed_even_if_all_artifacts_are_rehashed(tmp_path, monkeypatch):
    from overfit.evaluation import mcq_v2
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path))
    client = Client([review()])
    original = client.chat.completions.create

    def with_usage(**request):
        response = original(**request)
        response.usage = SimpleNamespace(completion_tokens=30, prompt_tokens=40, total_tokens=70,
                                         completion_tokens_details=SimpleNamespace(reasoning_tokens=10))
        return response

    client.chat.completions.create = with_usage
    result = runner.run_mcq_judge_v2(tmp_path, execute=True, settings=settings(), client_factory=lambda _: client)
    root = Path(result["judgment_dir"])
    events = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
    del events[1]["response"]["usage"]["visible_completion_tokens"]
    (root / "calls.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
    metrics = json.loads((root / "metrics.json").read_text())
    del metrics["usage"]["visible_completion_tokens"]
    (root / "metrics.json").write_text(json.dumps(metrics) + "\n")
    rows = [json.loads(line) for line in (root / "judgments.jsonl").read_text().splitlines()]
    (root / "report.md").write_text(mcq_v2._report(metrics, rows))
    for name in ("calls.jsonl", "metrics.json", "report.md"):
        _rehash_complete(root, name)
    with pytest.raises(runner.JudgeRunError):
        runner.replay_judgment(root)


def test_capability_gate_binds_frozen_generation_identity_and_exact_selection(tmp_path, monkeypatch):
    source = _source(tmp_path, item_count=5)
    source[2]["source"]["run_id"] = "e7b0d033-b90e-4399-a8ab-0232635410e2"
    monkeypatch.setattr(runner, "_mcq_source", lambda _: source)
    base = runner.run_mcq_judge(tmp_path)
    client = Client([review(), review()])
    result = runner.run_mcq_judge_capability(
        tmp_path, base_judgment=base["judgment_dir"], items="3,5", execute=True,
        settings=settings(), client_factory=lambda _: client,
    )
    manifest = json.loads((Path(result["judgment_dir"]) / "manifest.json").read_text())
    assert manifest["source_identity"]["run_id"] == "e7b0d033-b90e-4399-a8ab-0232635410e2"
    assert manifest["selected_items"] == [3, 5]
    assert result["metrics"]["max_calls"] == 4
    assert len(client.calls) == 2 and result["publication"] is None
    assert runner.replay_judgment(result["judgment_dir"])["status"] == "completed"
    with pytest.raises(runner.JudgeRunError):
        runner.run_mcq_judge_capability(tmp_path, base_judgment=base["judgment_dir"], items="3,4",
                                        execute=True, client_factory=lambda _: pytest.fail("wrong selection executed"))
    source[2]["source"]["run_id"] = "different-valid-generation"
    with pytest.raises(runner.JudgeRunError):
        runner.run_mcq_judge_capability(tmp_path, base_judgment=base["judgment_dir"], items="3,5",
                                        execute=True, client_factory=lambda _: pytest.fail("wrong source executed"))


def test_source_mutation_during_final_call_prevents_publication(tmp_path, monkeypatch):
    source = _source(tmp_path)
    monkeypatch.setattr(runner, "_mcq_source", lambda _: source)
    client = Client([review()])
    original = client.chat.completions.create

    def mutate(**request):
        result = original(**request)
        source[2]["source"]["trace_sha256"] = "c" * 64
        return result

    client.chat.completions.create = mutate
    with pytest.raises(runner.JudgeRunError):
        runner.run_mcq_judge_v2(tmp_path, execute=True, settings=settings(), client_factory=lambda _: client)
    assert not list((tmp_path / "judgments").glob("*/judged_mock_exam.md"))
    assert not list((tmp_path / "judgments").glob("*/complete.json"))


def test_base_mutation_during_final_call_prevents_calibration_completion(tmp_path, monkeypatch):
    base = base_bundle(tmp_path, monkeypatch)
    client = Client([review()])
    original = client.chat.completions.create

    def mutate(**request):
        result = original(**request)
        (base / "report.md").write_text("mutated frozen comparison")
        return result

    client.chat.completions.create = mutate
    with pytest.raises(runner.JudgeRunError):
        runner.run_mcq_judge_review(tmp_path, base_judgment=base, items="1", execute=True,
                                    settings=settings(), client_factory=lambda _: client)
    assert len(list((tmp_path / "judgments").glob("*/complete.json"))) == 1


def test_failed_completion_write_removes_uncommitted_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path))
    original = runner.Recording.write

    def fail_complete(self, name, content):
        if name == "complete.json":
            raise runner.JudgeRunError("simulated disk failure")
        return original(self, name, content)

    monkeypatch.setattr(runner.Recording, "write", fail_complete)
    with pytest.raises(runner.JudgeRunError):
        runner.run_mcq_judge_v2(tmp_path, execute=True, settings=settings(),
                              client_factory=lambda _: Client([review()]))
    assert not list((tmp_path / "judgments").glob("*/judged_mock_exam.md"))
    assert not list((tmp_path / "judgments").glob("*/judged_answers.md"))
    assert list((tmp_path / "judgments").glob("*/calls.jsonl"))


def test_usage_aggregate_overflow_cannot_leave_uncommitted_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path, item_count=2))
    client = Client([review(), review()])
    original = client.chat.completions.create

    def enormous_usage(**request):
        response = original(**request)
        response.usage = SimpleNamespace(prompt_tokens=1e308)
        return response

    client.chat.completions.create = enormous_usage
    with pytest.raises(runner.JudgeRunError, match="finite numeric limits"):
        runner.run_mcq_judge_v2(tmp_path, execute=True, settings=settings(), client_factory=lambda _: client)
    assert len(client.calls) == 2
    assert not list((tmp_path / "judgments").glob("*/judged_mock_exam.md"))
    assert not list((tmp_path / "judgments").glob("*/judged_answers.md"))
    assert not list((tmp_path / "judgments").glob("*/complete.json"))


def test_full_publication_excludes_material_failures_and_pending_before_rebalancing(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path, item_count=3))
    client = Client([review(), review(derived=False), review(pending=True)])
    result = runner.run_mcq_judge_v2(tmp_path, execute=True, settings=settings(), client_factory=lambda _: client)
    assert result["status"] == "completed"
    assert result["metrics"]["accepted_count"] == len(result["accepted_items"]) == 1
    assert result["metrics"]["mandatory_failure_item_count"] == 1
    assert result["metrics"]["human_review_pending_item_count"] == 1
    root = Path(result["judgment_dir"])
    assert json.loads((root / "review_queue.jsonl").read_text()) == {"item_index": 3, "status": "pending"}
    assert runner.replay_judgment(root)["metrics"]["accepted_count"] == 1
    with pytest.raises(runner.JudgeRunError, match="every retained item"):
        runner.run_mcq_judge_v2(tmp_path, max_items=2, execute=True,
                              client_factory=lambda _: pytest.fail("partial formal client constructed"))


@pytest.mark.parametrize("artifact,mutation", [
    ("manifest.json", "digest"),
    ("manifest.json", "mixed_protocol"),
    ("manifest.json", "boolean_selection"),
    ("manifest.json", "float_ceiling"),
    ("judged_mock_exam.md", "text"),
    ("judged_answers.md", "text"),
    ("report.md", "text"),
    ("review_queue.jsonl", "queue"),
])
def test_replay_rejects_derived_artifact_and_protocol_tampering(tmp_path, monkeypatch, artifact, mutation):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path))
    result = runner.run_mcq_judge_v2(tmp_path, execute=True, settings=settings(),
                                  client_factory=lambda _: Client([review()]))
    root = Path(result["judgment_dir"])
    path = root / artifact
    if artifact == "manifest.json":
        value = json.loads(path.read_text())
        if mutation == "digest":
            value["protocol_digest"] = "0" * 64
        elif mutation == "mixed_protocol":
            value["protocol_version"] = "mcq-judge-v1"
        elif mutation == "boolean_selection":
            value["selected_items"] = [True]
        else:
            value["max_calls"] = 2.0
        path.write_text(json.dumps(value) + "\n")
    else:
        path.write_text('{"item_index":1,"status":"pending"}\n' if mutation == "queue" else "forged contents")
    _rehash_complete(root, artifact)
    with pytest.raises(runner.JudgeRunError):
        runner.replay_judgment(root)
