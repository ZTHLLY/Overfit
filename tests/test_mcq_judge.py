"""Offline fake-client tests for the explicit mcq-v1 Judge gate."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from overfit.evaluation import runner
from overfit.evaluation.judge import JudgeSettings


def _item():
    return {
        "topic": "T",
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


def _review(*, pending=False, positive=True):
    return {
        "human_review_required": pending,
        "answerable": positive,
        "one_best_answer": positive,
        "correct_option_supported": positive,
        "other_options_incorrect": {
            label: (False if label == "A" else positive) for label in "ABCD"
        },
        "explanations_accurate": {label: positive for label in "ABCD"},
        "citations_supported": {label: positive for label in "ABCD"},
        "positive_stem": positive,
        "no_calculation": positive,
        "reason": "Fixture review.",
    }


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = next(self.responses)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=content, refusal=None),
                finish_reason="stop",
            )],
            usage=None,
            model="judge-fixture",
        )


class FailingClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **_kwargs):
        raise RuntimeError("fixture failure")


def _settings():
    return JudgeSettings(
        _env_file=None,
        base_url="https://judge.example/v1",
        api_key="fixture",
        model="judge-fixture",
        max_attempts=2,
    )


def _source(tmp_path, item_count=1):
    chunks = [{
        "id": "c1", "text": "Evidence", "source": "notes.pdf", "page": 5,
        "page_end": None, "section": None, "provenance": None,
    }, {
        "id": "c2", "text": "Irrelevant other batch", "source": "other.pdf", "page": 9,
        "page_end": None, "section": None, "provenance": None,
    }]
    tasks = {
        "schema_version": 1,
        "generation_contract": "mcq-v1",
        "source": {"run_id": "run", "manifest_sha256": "a" * 64, "trace_sha256": "b" * 64},
        "course": "COURSE",
        "chunks": chunks,
        "item_materials": [[chunks[0]] for _ in range(item_count)],
        "items": [_item() for _ in range(item_count)],
        "generation_model": "generator-fixture",
    }
    return tmp_path, {"generation_contract": "mcq-v1"}, tasks


def _rehash_complete(root: Path, name: str):
    complete_path = root / "complete.json"
    complete = json.loads(complete_path.read_text())
    complete["artifacts"][name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    complete_path.write_text(json.dumps(complete, sort_keys=True, separators=(",", ":")) + "\n")


def test_mcq_judge_derives_ceiling_and_publishes_only_complete_positive_items(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path))
    client = Client([json.dumps(_review())])

    result = runner.run_mcq_judge(
        tmp_path, execute=True, settings=_settings(), client_factory=lambda _: client
    )

    assert result["status"] == "completed"
    assert result["metrics"]["max_calls"] == 2
    assert result["metrics"]["call_starts"] == 1
    assert len(result["accepted_items"]) == 1
    assert result["publication"] is not None
    assert "Which statement is supported?" in Path(
        result["publication"]["paper"]
    ).read_text()
    assert client.calls[0]["response_format"] == {"type": "json_object"}
    assert "Irrelevant other batch" not in json.dumps(client.calls[0])
    replay = runner.replay_judgment(result["judgment_dir"])
    assert replay["status"] == "completed"
    assert replay["metrics"]["accepted_count"] == 1


def test_pending_item_is_review_only_and_invalid_responses_make_run_incomplete(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path))
    pending = Client([json.dumps(_review(pending=True))])
    reviewed = runner.run_mcq_judge(
        tmp_path, execute=True, settings=_settings(), client_factory=lambda _: pending
    )
    assert reviewed["status"] == "completed"
    assert reviewed["accepted_items"] == []
    assert reviewed["metrics"]["human_review_pending_item_count"] == 1

    # Use a fresh source root because judge recordings are append-only children.
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(other))
    invalid = Client(["{}", "{}"])
    failed = runner.run_mcq_judge(
        other, execute=True, settings=_settings(), client_factory=lambda _: invalid
    )
    assert failed["status"] == "incomplete"
    assert failed["accepted_items"] == []
    assert failed["metrics"]["call_starts"] == failed["metrics"]["max_calls"] == 2


def test_mcq_judge_replay_rejects_ceiling_and_unsafe_json_tampering(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path))
    result = runner.run_mcq_judge(
        tmp_path,
        execute=True,
        settings=_settings(),
        client_factory=lambda _: Client([json.dumps(_review())]),
    )
    root = Path(result["judgment_dir"])

    manifest_path = root / "manifest.json"
    original = manifest_path.read_text()
    manifest = json.loads(original)
    manifest["max_calls"] = 999
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    _rehash_complete(root, "manifest.json")
    try:
        runner.replay_judgment(root)
    except runner.JudgeRunError:
        pass
    else:
        raise AssertionError("tampered MCQ Judge ceiling was accepted")

    manifest_path.write_text(original[:-2] + ',"max_calls":2}\n')
    _rehash_complete(root, "manifest.json")
    try:
        runner.replay_judgment(root)
    except runner.JudgeRunError:
        pass
    else:
        raise AssertionError("duplicate-key MCQ Judge JSON was accepted")


def test_mcq_judge_replay_rejects_attempt_after_success(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path))
    result = runner.run_mcq_judge(
        tmp_path,
        execute=True,
        settings=_settings(),
        client_factory=lambda _: Client([json.dumps(_review())]),
    )
    root = Path(result["judgment_dir"])
    calls_path = root / "calls.jsonl"
    events = [json.loads(line) for line in calls_path.read_text().splitlines()]
    request = events[0]["request"]
    content = events[1]["response"]
    stamp = datetime.now(UTC).isoformat()
    events.extend([
        {
            "schema_version": 4, "judgment_id": root.name, "seq": 3,
            "time": stamp, "type": "call_started", "call_id": "extra",
            "item_index": 1, "attempt": 2, "request": request,
        },
        {
            "schema_version": 4, "judgment_id": root.name, "seq": 4,
            "time": stamp, "type": "call_finished", "call_id": "extra",
            "item_index": 1, "attempt": 2, "status": "response",
            "response": content, "error": None, "retryable": False,
            "elapsed_seconds": 0.0,
        },
    ])
    calls_path.write_text("".join(json.dumps(event) + "\n" for event in events))
    _rehash_complete(root, "calls.jsonl")
    try:
        runner.replay_judgment(root)
    except runner.JudgeRunError:
        pass
    else:
        raise AssertionError("MCQ Judge replay accepted an attempt after success")


def test_mcq_judge_replay_rejects_boolean_config_and_forged_report(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path))
    result = runner.run_mcq_judge(
        tmp_path,
        execute=True,
        settings=_settings(),
        client_factory=lambda _: Client([json.dumps(_review())]),
    )
    root = Path(result["judgment_dir"])

    report_path = root / "report.md"
    original_report = report_path.read_text()
    report_path.write_text("forged report")
    _rehash_complete(root, "report.md")
    try:
        runner.replay_judgment(root)
    except runner.JudgeRunError:
        pass
    else:
        raise AssertionError("forged MCQ Judge report was accepted")

    report_path.write_text(original_report.replace("Calls: 1/2", "Calls: 1/1"))
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["judge_config"]["max_attempts"] = True
    manifest["max_calls"] = 1
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    metrics_path = root / "metrics.json"
    metrics = json.loads(metrics_path.read_text())
    metrics["max_calls"] = 1
    metrics_path.write_text(json.dumps(metrics, sort_keys=True, separators=(",", ":")) + "\n")
    for name in ("report.md", "manifest.json", "metrics.json"):
        _rehash_complete(root, name)
    try:
        runner.replay_judgment(root)
    except runner.JudgeRunError:
        pass
    else:
        raise AssertionError("boolean MCQ Judge max_attempts was accepted")


def test_mcq_source_rejects_post_inspection_bytes_and_duplicate_json(tmp_path, monkeypatch):
    chunks = [{
        "id": "c1", "text": "Evidence", "source": "notes.pdf", "page": 5,
        "page_end": None, "section": None, "provenance": None,
    }]
    manifest = {
        "task": {"course": "COURSE"},
        "config": {"generation_model": "fixture"},
    }
    trace = "".join([
        json.dumps({
            "event_type": "materials_selected",
            "payload": {"batch_index": 1, "chunks": chunks},
        }) + "\n",
        json.dumps({
            "event_type": "options_balanced",
            "payload": {"item_material_batches": [1]},
        }) + "\n",
    ]).encode()
    manifest_raw = json.dumps(manifest).encode()
    inspected = {
        "generation_contract": "mcq-v1", "trace_integrity": "complete",
        "run_status": "completed", "items": [_item()], "run_id": "run",
        "manifest_sha256": "0" * 64, "trace_sha256": runner.digest(trace),
    }
    monkeypatch.setattr(runner, "inspect_run", lambda _: inspected)
    monkeypatch.setattr(
        runner, "_read",
        lambda _root, name: manifest_raw if name == "manifest.json" else trace,
    )
    try:
        runner._mcq_source(tmp_path)
    except runner.JudgeRunError:
        pass
    else:
        raise AssertionError("MCQ source accepted bytes different from inspected digests")

    duplicate = b'{"task":{"course":"COURSE"},"task":{"course":"COURSE"},"config":{"generation_model":"fixture"}}'
    inspected["manifest_sha256"] = runner.digest(duplicate)
    monkeypatch.setattr(
        runner, "_read",
        lambda _root, name: duplicate if name == "manifest.json" else trace,
    )
    try:
        runner._mcq_source(tmp_path)
    except runner.JudgeRunError:
        pass
    else:
        raise AssertionError("MCQ source accepted duplicate-key JSON")


def test_mcq_judge_replay_rejects_manifest_envelope_tampering(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path))
    result = runner.run_mcq_judge(
        tmp_path,
        execute=True,
        settings=_settings(),
        client_factory=lambda _: Client([json.dumps(_review())]),
    )
    root = Path(result["judgment_dir"])
    manifest_path = root / "manifest.json"
    original = json.loads(manifest_path.read_text())
    mutations = [
        {**original, "untrusted_extra": True},
        {**original, "schema_version": 2},
        {**original, "judgment_id": "different"},
        {**original, "source_identity": {**original["source_identity"], "run_id": "other"}},
        {**original, "created_at": "2026-11-04T00:00:00"},
        {key: value for key, value in original.items() if key != "max_items"},
    ]
    for manifest in mutations:
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        )
        _rehash_complete(root, "manifest.json")
        try:
            runner.replay_judgment(root)
        except runner.JudgeRunError:
            pass
        else:
            raise AssertionError("MCQ Judge replay accepted a mutated manifest envelope")

    manifest_path.write_text(
        json.dumps(original, sort_keys=True, separators=(",", ":")) + "\n"
    )
    _rehash_complete(root, "manifest.json")
    complete_path = root / "complete.json"
    original_complete = json.loads(complete_path.read_text())
    complete_path.write_text(
        json.dumps(
            {**original_complete, "schema_version": True},
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
    )
    try:
        runner.replay_judgment(root)
    except runner.JudgeRunError:
        pass
    else:
        raise AssertionError("MCQ Judge replay accepted boolean complete schema_version")

    complete_path.write_text(
        json.dumps(original_complete, sort_keys=True, separators=(",", ":")) + "\n"
    )
    tasks_path = root / "tasks.json"
    tasks = json.loads(tasks_path.read_text())
    tasks["schema_version"] = True
    tasks_path.write_text(json.dumps(tasks, sort_keys=True, separators=(",", ":")) + "\n")
    _rehash_complete(root, "tasks.json")
    try:
        runner.replay_judgment(root)
    except runner.JudgeRunError:
        pass
    else:
        raise AssertionError("MCQ Judge replay accepted boolean tasks schema_version")


def test_mcq_judge_replay_binds_source_snapshot_with_json_types(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path))
    result = runner.run_mcq_judge(tmp_path, execute=False)
    root = Path(result["judgment_dir"])

    tasks_path = root / "tasks.json"
    tasks = json.loads(tasks_path.read_text())
    tasks["items"][0]["options"]["A"]["page"] = 5.0
    tasks_path.write_text(json.dumps(tasks, sort_keys=True, separators=(",", ":")) + "\n")

    judgments_path = root / "judgments.jsonl"
    rows = [json.loads(line) for line in judgments_path.read_text().splitlines()]
    rows[0]["item"]["options"]["A"]["page"] = 5.0
    judgments_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    for name in ("tasks.json", "judgments.jsonl"):
        _rehash_complete(root, name)

    try:
        runner.replay_judgment(root)
    except runner.JudgeRunError:
        pass
    else:
        raise AssertionError("MCQ Judge replay accepted typed source snapshot tampering")


def test_mcq_judge_replay_requires_strict_call_event_envelopes(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path))
    result = runner.run_mcq_judge(
        tmp_path,
        execute=True,
        settings=_settings(),
        client_factory=lambda _: Client([json.dumps(_review())]),
    )
    root = Path(result["judgment_dir"])
    calls_path = root / "calls.jsonl"
    events = [json.loads(line) for line in calls_path.read_text().splitlines()]
    for event in events:
        for field in ("schema_version", "seq", "attempt"):
            event[field] = float(event[field])
    calls_path.write_text("".join(json.dumps(event) + "\n" for event in events))
    _rehash_complete(root, "calls.jsonl")
    try:
        runner.replay_judgment(root)
    except runner.JudgeRunError:
        pass
    else:
        raise AssertionError("MCQ Judge replay accepted non-integer call event fields")


def test_mcq_judge_replay_rejects_unsafe_nested_finish_payloads(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(tmp_path))
    result = runner.run_mcq_judge(
        tmp_path,
        execute=True,
        settings=_settings(),
        client_factory=lambda _: Client([json.dumps(_review())]),
    )
    root = Path(result["judgment_dir"])
    calls_path = root / "calls.jsonl"
    events = [json.loads(line) for line in calls_path.read_text().splitlines()]
    finish = next(event for event in events if event["type"] == "call_finished")
    finish["response"]["private_provider_metadata"] = "x"
    finish["error"] = {"unexpected": "ignored"}
    calls_path.write_text("".join(json.dumps(event) + "\n" for event in events))
    _rehash_complete(root, "calls.jsonl")
    try:
        runner.replay_judgment(root)
    except runner.JudgeRunError:
        pass
    else:
        raise AssertionError("MCQ Judge replay accepted unsafe response metadata/error")

    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(runner, "_mcq_source", lambda _: _source(other))
    failed = runner.run_mcq_judge(
        other,
        execute=True,
        settings=_settings(),
        client_factory=lambda _: FailingClient(),
    )
    failed_root = Path(failed["judgment_dir"])
    failed_calls = failed_root / "calls.jsonl"
    events = [json.loads(line) for line in failed_calls.read_text().splitlines()]
    finish = next(event for event in events if event["type"] == "call_finished")
    finish["response"] = {
        "content": "hidden", "usage": None, "finish_reason": "stop", "model": "x",
    }
    finish["error"] = {"type": "made_up", "message": "unsafe"}
    failed_calls.write_text("".join(json.dumps(event) + "\n" for event in events))
    _rehash_complete(failed_root, "calls.jsonl")
    try:
        runner.replay_judgment(failed_root)
    except runner.JudgeRunError:
        pass
    else:
        raise AssertionError("MCQ Judge replay accepted response on an error finish")
