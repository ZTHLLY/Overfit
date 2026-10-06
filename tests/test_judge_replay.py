"""Tamper/fault fixtures for judge reconstruction; never contacts a provider."""

import json
from pathlib import Path

import pytest

from overfit.evaluation import judge, runner
from tests.test_eval_replay import fixture_run
from tests.test_judge_cli import install_fake_judge


@pytest.fixture
def completed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = fixture_run(tmp_path).run_dir
    calls = install_fake_judge(monkeypatch)
    result = runner.run_judge(source, execute=True, max_calls=2)
    assert result["status"] == "completed" and len(calls) == 2
    return Path(result["judgment_dir"])


def rehash(root):
    path = root / "complete.json"
    complete = json.loads(path.read_text())
    complete["artifacts"] = {name: runner.digest((root / name).read_bytes()) for name in runner.ARTIFACTS}
    path.write_text(json.dumps(complete))


def edit_events(root, change):
    path = root / "calls.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()]
    change(events)
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    rehash(root)


def test_repeat_offline_replay_reconstructs_original_results(completed, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No runtime configuration or services in replay")
    monkeypatch.setattr(judge, "JudgeSettings", forbidden)
    monkeypatch.setattr(judge, "make_client", forbidden)
    monkeypatch.setattr(judge, "invoke", forbidden)
    originals = {p.name: p.read_bytes() for p in completed.iterdir() if p.is_file()}
    first, second = runner.replay_judgment(completed), runner.replay_judgment(completed)
    assert first["exit_code"] == 0 and first["metrics"]["model_all_positive_count"] == 1
    assert {k: v for k, v in first["metrics"].items() if k not in {"replay_id", "replayed_at"}} == {
        k: v for k, v in second["metrics"].items() if k not in {"replay_id", "replayed_at"}}
    assert first["replay_dir"] != second["replay_dir"]
    assert originals == {n: (completed / n).read_bytes() for n in originals}


def test_stale_derived_metrics_and_judgments_are_never_used(completed):
    (completed / "metrics.json").write_text('{"model_all_positive_count": 9000}')
    (completed / "judgments.jsonl").write_text('not even normalized output\n')
    rehash(completed)
    result = runner.replay_judgment(completed)
    assert result["metrics"]["model_all_positive_count"] == 1
    rows = [json.loads(line) for line in (Path(result["replay_dir"]) / "judgments.jsonl").read_text().splitlines()]
    assert rows[0]["answerability"]["verification"]["evidence_records"][0]["start"] == 0


@pytest.mark.parametrize("mutate", [
    lambda es: es[0].update(schema_version=True),
    lambda es: es[0].update(schema_version=2),
    lambda es: es[0].update(seq=99),
    lambda es: es[0].update(judgment_id="other"),
    lambda es: es[0].update(attempt=2),
    lambda es: es[0].update(item_id="invented"),
    lambda es: es[0].update(phase="support"),
    lambda es: es[0].update(cache_key="wrong"),
    lambda es: es[0]["request"]["messages"].append({"role": "assistant", "content": "leaked answer"}),
    lambda es: es[1].update(call_id="unpaired"),
    lambda es: es[1].update(elapsed_seconds=float("inf")),
    lambda es: es[1]["response"].update(usage={"total_tokens": True}),
    lambda es: es[2]["result"].update(status="error"),
    lambda es: es[3].update(call_id=es[0]["call_id"]),
    lambda es: es[4]["response"].update(content='{"fabricated":"valid"}'),
    lambda es: es.pop(2),
    lambda es: es.append(es[-1]),
])
def test_bad_events_rejected_even_with_recomputed_artifact_hashes(completed, mutate):
    edit_events(completed, mutate)
    with pytest.raises(runner.JudgeRunError):
        runner.replay_judgment(completed)
    assert not (completed / "replays").exists()


@pytest.mark.parametrize("name", sorted(runner.ARTIFACTS | {"complete.json"}))
def test_missing_or_symlinked_files_rejected(completed, tmp_path, name):
    path = completed / name
    outside = tmp_path / "outside"
    path.rename(outside)
    with pytest.raises(runner.JudgeRunError):
        runner.replay_judgment(completed)
    path.symlink_to(outside)
    with pytest.raises(runner.JudgeRunError):
        runner.replay_judgment(completed)


def test_modified_file_without_updated_digest_rejected(completed):
    with (completed / "calls.jsonl").open("a") as out:
        out.write("malformed")
    with pytest.raises(runner.JudgeRunError, match="digest mismatch"):
        runner.replay_judgment(completed)


def test_valid_hash_does_not_rescue_truncated_log(completed):
    path = completed / "calls.jsonl"
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    rehash(completed)
    with pytest.raises(runner.JudgeRunError, match="Truncated"):
        runner.replay_judgment(completed)


@pytest.mark.parametrize("field,value", [("schema_version", 2), ("protocol_digest", "unknown"),
                                        ("same_model_as_generator", True), ("max_calls", 1),
                                        ("max_calls", True), ("max_items", 0)])
def test_manifest_contracts_cannot_be_changed_silently(completed, field, value):
    path = completed / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    rehash(completed)
    with pytest.raises(runner.JudgeRunError):
        runner.replay_judgment(completed)


def test_terminal_cannot_claim_a_different_state(completed):
    path = completed / "complete.json"
    complete = json.loads(path.read_text())
    complete["status"] = "prepared"
    path.write_text(json.dumps(complete))
    with pytest.raises(runner.JudgeRunError):
        runner.replay_judgment(completed)


def test_artifact_traversal_is_rejected_before_read(completed):
    path = completed / "complete.json"
    complete = json.loads(path.read_text())
    complete["artifacts"]["../outside"] = "0" * 64
    path.write_text(json.dumps(complete))
    with pytest.raises(runner.JudgeRunError):
        runner.replay_judgment(completed)


def test_output_parent_symlink_refused(completed, tmp_path):
    (completed / "replays").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(runner.JudgeRunError):
        runner.replay_judgment(completed)


def test_source_must_still_match(completed):
    source = completed.parent.parent
    (source / "FIXTURE_answers.md").write_text("changed source artifact")
    with pytest.raises(runner.JudgeRunError):
        runner.replay_judgment(completed)


def test_initial_source_snapshot_is_not_trusted_over_source(completed):
    path = completed / "tasks.json"
    tasks = json.loads(path.read_text())
    tasks["chunks"][0]["text"] = "secret new evidence"
    path.write_text(json.dumps(tasks))
    rehash(completed)
    with pytest.raises(runner.JudgeRunError):
        runner.replay_judgment(completed)


def test_no_terminal_does_not_become_completed(completed):
    (completed / "complete.json").unlink()
    with pytest.raises(runner.JudgeRunError):
        runner.replay_judgment(completed)


@pytest.mark.parametrize("change", [
    lambda result: result["model_review"].update(verdict="unknown"),
    lambda result: result["human_review"].update(status="pending"),
    lambda result: result["verification"]["evidence_records"][0].update(start=999),
    lambda result: result.update(raw_response="forged"),
])
def test_replay_rederives_all_layers_instead_of_trusting_task_results(completed, change):
    edit_events(completed, lambda events: change(events[2]["result"]))
    with pytest.raises(runner.JudgeRunError, match="raw judge response"):
        runner.replay_judgment(completed)


@pytest.mark.parametrize("protocol", ["judge-v1", "judge-v2"])
def test_old_protocol_is_rejected_without_migrating_old_artifacts(completed, protocol):
    path = completed / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["protocol_version"] = protocol
    path.write_text(json.dumps(manifest))
    rehash(completed)
    before = {p.name: p.read_bytes() for p in completed.iterdir() if p.is_file()}
    with pytest.raises(runner.JudgeRunError, match="Unsupported judge protocol"):
        runner.replay_judgment(completed)
    assert before == {p.name: p.read_bytes() for p in completed.iterdir() if p.is_file()}
    assert not (completed / "replays").exists()


@pytest.mark.parametrize("module", ["judge.py", "runner.py", "metrics.py"])
def test_incompatible_checker_implementation_is_rejected(completed, module):
    path = completed / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["checker_identity"][module] = "0" * 64
    path.write_text(json.dumps(manifest))
    rehash(completed)
    with pytest.raises(runner.JudgeRunError, match="Unsupported judge protocol"):
        runner.replay_judgment(completed)
    assert not (completed / "replays").exists()


@pytest.mark.parametrize("version", [1, 2])
def test_real_legacy_artifact_schema_is_explicitly_unsupported(completed, version):
    path = completed / "complete.json"
    complete = json.loads(path.read_text())
    complete["schema_version"] = version
    path.write_text(json.dumps(complete))
    before = {p.name: p.read_bytes() for p in completed.iterdir() if p.is_file()}
    with pytest.raises(runner.JudgeRunError, match="Unsupported judge recording version"):
        runner.replay_judgment(completed)
    assert before == {p.name: p.read_bytes() for p in completed.iterdir() if p.is_file()}
    assert not (completed / "replays").exists()


def test_stale_human_queue_is_recomputed_from_raw_model_response(completed):
    path = completed / "review_queue.jsonl"
    path.write_text('{"item_id":"invented","status":"pending"}\n')
    rehash(completed)
    replay = runner.replay_judgment(completed)
    assert replay["metrics"]["human_review_pending_item_count"] == 0
    assert Path(replay["replay_dir"], "review_queue.jsonl").read_text() == ""
    assert "invented" in path.read_text()
