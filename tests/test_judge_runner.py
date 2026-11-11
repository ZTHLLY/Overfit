"""End-to-end offline judge scheduling, accounting and fail-closed persistence."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from overfit.evaluation import judge, runner
from overfit.evaluation.citations import check_citation
from overfit.evaluation.report import render_report, summarize
from overfit.evaluation.trace import TraceWriter
from tests.test_eval_replay import digest, fixture_run


def make_source(tmp_path, count=1, **kwargs):
    if count == 1:
        return fixture_run(tmp_path, **kwargs).run_dir
    writer = TraceWriter.create(tmp_path, {"course": "F", "questions": count}, {})
    chunks = [{"id": "c1", "source": "notes.pdf", "page": 5, "page_end": 5,
               "section": None, "text": "Synthetic evidence"}]
    writer.emit("materials_selected", {"chunks": chunks, "materials_sha256": digest(chunks),
                                       "selection": {"actual_count": 1}, "index": {}})
    call = writer.start_call(1, 1, {"model": "fixture", "stream": True, "temperature": 0,
                                    "timeout": 30, "response_format": None,
                                    "messages": [{"role": "user", "content": "prompt"}]})
    items = [{"topic": "T", "question": "Q", "answer": "A", "source": "notes.pdf", "page": 5}
             for _ in range(count)]
    writer.emit("call_finished", {"call_id": call, "status": "completed", "content": json.dumps({"items": items}),
                                  "response_complete": True, "elapsed_seconds": 0, "error": None, "usage": None})
    writer.emit("validation_finished", {"call_id": call, "status": "valid", "item_count": count, "error": None})
    for index, item in enumerate(items, 1):
        writer.emit("citation_processed", {"call_id": call, "item_id": f"{call}-item-{index}",
                    "original_index": index, "final_index": index, "pre_policy": item,
                    "post_policy": item, "action": "keep", "reason": "fixture",
                    "pre_check": check_citation(item, chunks), "post_check": check_citation(item, chunks)})
    result = summarize(writer.manifest, writer.events)
    writer.write_artifact("result.json", json.dumps(result))
    writer.write_artifact("report.md", render_report(result))
    writer.write_artifact("F_mock_exam.md", "exam")
    writer.write_artifact("F_answers.md", "answers")
    writer.finish("completed", "complete", "retained")
    return writer.run_dir


def body(phase, verdict=None):
    evidence = [{"chunk_id": "c1", "quote": "Synthetic evidence"}]
    if phase == "answerability":
        return {"verdict": verdict or "answerable", "reason": "A short reason.",
                "evidence": evidence if verdict in {None, "answerable"} else []}
    support_verdict = verdict or "supported"
    return {"material_support": support_verdict, "citation_support": support_verdict,
            "reason": "A short reason.", "citation_evidence": evidence if verdict is None else [],
            "claims": [{"text": "A", "verdict": "supported", "evidence": evidence}] if verdict is None else []}


def response(content, finish="stop", refusal=None):
    return NS(choices=[NS(message=NS(content=json.dumps(content) if not isinstance(content, str) else content,
                                    refusal=refusal, reasoning_content="INTERNAL_REASONING"), finish_reason=finish)],
              usage=NS(prompt_tokens=10, completion_tokens=5, total_tokens=15), model="fixture-judge")


class Fake:
    def __init__(self, actions=None):
        self.actions = iter(actions) if actions is not None else None
        self.calls = []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **request):
        self.calls.append(copy.deepcopy(request))
        if self.actions is not None:
            action = next(self.actions)
            if isinstance(action, BaseException):
                raise action
            return action
        payload = json.loads(request["messages"][1]["content"])
        return response(body("support" if "answer" in payload else "answerability"))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = judge.JudgeSettings(base_url="https://fixture.invalid/v1", api_key="PRIVATE_JUDGE_KEY",
                                   model="fixture-judge", _env_file=None)
    return NS(root=tmp_path, settings=settings, source=make_source(tmp_path))


def execute(setup, fake=None, **kwargs):
    fake = fake or Fake()
    options = {"execute": True, "max_calls": 10, "settings": setup.settings,
               "client_factory": lambda settings: fake, **kwargs}
    return runner.run_judge(setup.source, **options), fake


def rows(result):
    return [json.loads(line) for line in Path(result["judgment_dir"], "judgments.jsonl").read_text().splitlines()]


def events(result):
    return [json.loads(line) for line in Path(result["judgment_dir"], "calls.jsonl").read_text().splitlines()]


def test_positive_two_calls_preserves_source_and_records_before_call(setup):
    before = {p.name: p.read_bytes() for p in setup.source.iterdir()}
    fake = Fake()
    original = fake.chat.completions.create
    def create(**request):
        directory = next((setup.source / "judgments").iterdir())
        log = [json.loads(line) for line in (directory / "calls.jsonl").read_text().splitlines()]
        assert log[-1]["type"] == "call_started"
        assert log[-1]["request"] == request
        assert (directory / "manifest.json").exists() and (directory / "tasks.json").exists()
        return original(**request)
    fake.chat.completions.create = create
    result, _ = execute(setup, fake)
    assert result["status"] == "completed" and result["exit_code"] == 0
    assert result["metrics"]["model_all_positive_count"] == 1
    assert len(fake.calls) == result["metrics"]["call_count"] == 2
    assert result["metrics"]["usage"] == {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
    assert before == {name: (setup.source / name).read_bytes() for name in before}
    stored = "".join(p.read_text() for p in Path(result["judgment_dir"]).iterdir())
    assert "PRIVATE_JUDGE_KEY" not in stored and "INTERNAL_REASONING" not in stored
    replay = runner.replay_judgment(result["judgment_dir"])
    assert {key: replay["metrics"][key] for key in result["metrics"]} == result["metrics"]


@pytest.mark.parametrize("answer,support", [("not_answerable", "unsupported"), ("unknown", "unknown")])
def test_valid_negative_and_unknown_are_not_retried(setup, answer, support):
    fake = Fake([response(body("answerability", answer)), response(body("support", support))])
    result, _ = execute(setup, fake)
    assert result["status"] == "completed" and len(fake.calls) == 2
    assert result["metrics"]["model_all_positive_count"] == 0
    assert result["metrics"]["retry_count"] == 0
    assert rows(result)[0]["answerability"]["model_review"]["verdict"] == answer


@pytest.mark.parametrize("first", [response("invalid JSON"), TimeoutError("SECRET SDK ERROR")])
def test_schema_and_transient_failure_retry_same_request_with_budget(setup, first):
    fake = Fake([first, response(body("answerability")), response(body("support"))])
    result, _ = execute(setup, fake, max_calls=3)
    assert result["status"] == "completed" and len(fake.calls) == 3
    assert fake.calls[0] == fake.calls[1]
    assert result["metrics"]["retry_count"] == 1
    assert [e["attempt"] for e in events(result) if e["type"] == "call_started"] == [1, 2, 1]
    stored = Path(result["judgment_dir"], "calls.jsonl").read_text()
    assert "SECRET SDK ERROR" not in stored
    assert runner.replay_judgment(result["judgment_dir"])["exit_code"] == 0


def test_invalid_evidence_is_not_retried_or_converted_to_negative(setup):
    bad = body("answerability")
    bad["evidence"][0]["quote"] = "Invented quotation"
    fake = Fake([response(bad), response(body("support"))])
    result, _ = execute(setup, fake)
    assert len(fake.calls) == 2 and result["exit_code"] == 0
    state = rows(result)[0]["answerability"]
    assert state["status"] == "evaluated" and "review" not in state
    assert state["model_review"] == bad
    assert state["verification"]["evidence"] == "diagnostics"
    assert "outcome_counts" not in result["metrics"]
    assert result["metrics"]["retry_count"] == 0
    assert runner.replay_judgment(result["judgment_dir"])["exit_code"] == 0

    assert state["human_review"]["status"] == "not_required"
    assert result["metrics"]["evaluated_phase_count"] == 2
    assert result["metrics"]["model_all_positive_count"] == 1


def test_max_attempts_is_per_task_but_global_budget_spans_phases_and_items(setup):
    setup.source = make_source(setup.root / "two", count=2)
    fake = Fake([response("bad"), response(body("answerability")), response(body("support"))])
    result, _ = execute(setup, fake, max_calls=3)
    output = rows(result)
    assert len(fake.calls) == 3 and result["exit_code"] == 1
    assert result["metrics"]["model_all_positive_count"] == 1
    assert result["metrics"]["retained_count"] == 2
    assert result["metrics"]["retained_count"] == 2
    assert all(output[1][phase]["reason"] == "call_budget" for phase in runner.PHASES)
    assert runner.replay_judgment(result["judgment_dir"])["exit_code"] == 1


def test_schema_retry_limit_exhausts_without_extra_attempts(setup):
    fake = Fake([response("bad"), response("still bad"), response(body("support"))])
    result, _ = execute(setup, fake, max_calls=10)
    assert len(fake.calls) == 3
    assert rows(result)[0]["answerability"]["reason"] == "schema_error"
    assert result["metrics"]["retry_count"] == 1


def test_max_items_leaves_all_remaining_items_in_denominator(setup):
    setup.source = make_source(setup.root / "two", count=2)
    result, fake = execute(setup, max_items=1)
    assert len(fake.calls) == 2 and result["exit_code"] == 0
    assert result["metrics"]["retained_count"] == 2
    assert all(rows(result)[1][phase]["reason"] == "max_items" for phase in runner.PHASES)
    assert runner.replay_judgment(result["judgment_dir"])["exit_code"] == 0

    assert result["metrics"]["selected_evaluated_phase_count"] == result["metrics"]["selected_phase_count"] == 2
    assert result["metrics"]["planned_phase_count"] == 4


def test_input_limit_never_sends_truncated_material_or_constructs_client(setup):
    setup.settings = setup.settings.model_copy(update={"max_input_chars": 1})
    def forbidden(settings):
        raise AssertionError("Must not create a client for oversized input")
    result, fake = execute(setup, client_factory=forbidden)
    assert len(fake.calls) == 0 and result["exit_code"] == 1
    assert all(rows(result)[0][phase]["reason"] == "input_limit" for phase in runner.PHASES)
    tasks = json.loads(Path(result["judgment_dir"], "tasks.json").read_text())
    assert tasks["chunks"][0]["text"] == "Synthetic evidence"
    assert runner.replay_judgment(result["judgment_dir"])["exit_code"] == 1


@pytest.mark.parametrize("finish,refusal,reason", [("length", None, "truncated_response"),
                                                  ("stop", "refused", "refusal"),
                                                  (None, None, "truncated_response")])
def test_incomplete_responses_not_accepted_even_with_valid_json(setup, finish, refusal, reason):
    fake = Fake([response(body("answerability"), finish, refusal), response(body("support"))])
    result, _ = execute(setup, fake)
    assert len(fake.calls) == 2
    assert rows(result)[0]["answerability"]["reason"] == reason
    assert result["exit_code"] == 1 and result["metrics"]["model_all_positive_count"] == 0


def test_keyboard_interrupt_stops_all_future_calls_and_replays_130(setup):
    setup.source = make_source(setup.root / "two", count=2)
    result, fake = execute(setup, Fake([KeyboardInterrupt()]))
    assert len(fake.calls) == 1 and result["status"] == "interrupted" and result["exit_code"] == 130
    output = rows(result)
    assert output[0]["answerability"]["reason"] == "interrupted"
    assert output[0]["support"]["reason"] == "interrupted"
    assert all(output[1][phase]["reason"] == "interrupted" for phase in runner.PHASES)
    assert runner.replay_judgment(result["judgment_dir"])["exit_code"] == 130


def test_nontransient_provider_error_does_not_retry(setup):
    result, fake = execute(setup, Fake([RuntimeError("SECRET provider details"), response(body("support"))]))
    assert len(fake.calls) == 2 and result["exit_code"] == 1
    assert rows(result)[0]["answerability"]["reason"] == "provider_error"
    assert "SECRET provider details" not in Path(result["judgment_dir"], "calls.jsonl").read_text()


@pytest.mark.parametrize("name,call_count", [("manifest.json", 0), ("tasks.json", 0),
                                            ("calls.jsonl", 0), ("judgments.jsonl", 2),
                                            ("metrics.json", 2), ("report.md", 2), ("review_queue.jsonl", 2),
                                            ("complete.json", 2)])
def test_atomic_file_failure_stops_without_terminal_success(setup, monkeypatch, name, call_count):
    original = runner.os.replace
    def fail_replace(source, target):
        if Path(target).name == name:
            raise OSError("SECRET filesystem message")
        return original(source, target)
    monkeypatch.setattr(runner.os, "replace", fail_replace)
    fake = Fake()
    with pytest.raises(runner.JudgeRunError) as error:
        execute(setup, fake)
    assert "SECRET" not in str(error.value)
    assert len(fake.calls) == call_count
    directory = next((setup.source / "judgments").iterdir())
    assert not (directory / "complete.json").exists()
    with pytest.raises(runner.JudgeRunError):
        runner.replay_judgment(directory)


@pytest.mark.parametrize("kind,call_count", [("call_started", 0), ("call_finished", 1),
                                            ("task_finished", 1)])
def test_event_fsync_failure_stops_further_model_calls(setup, monkeypatch, kind, call_count):
    original = runner.os.fsync
    def fail_fsync(fd):
        for directory in (setup.source / "judgments").glob("*"):
            path = directory / "calls.jsonl"
            if path.exists() and runner.os.fstat(fd).st_ino == path.stat().st_ino:
                text = path.read_text()
                if text and json.loads(text.splitlines()[-1])["type"] == kind:
                    raise OSError("SECRET disk error")
        return original(fd)
    monkeypatch.setattr(runner.os, "fsync", fail_fsync)
    fake = Fake()
    with pytest.raises(runner.JudgeRunError) as error:
        execute(setup, fake)
    assert "SECRET" not in str(error.value)
    assert len(fake.calls) == call_count
    directory = next((setup.source / "judgments").iterdir())
    assert not (directory / "complete.json").exists()


@pytest.mark.parametrize("kwargs", [{"empty": True}, {"action": "drop"}])
def test_empty_or_all_dropped_has_no_calls_and_no_quality_pass(setup, kwargs):
    setup.source = make_source(setup.root / "empty", **kwargs)
    original = {p.name: p.read_bytes() for p in setup.source.iterdir()}
    result, fake = execute(setup)
    assert result["status"] == "completed" and result["exit_code"] == 0 and not fake.calls
    assert result["metrics"]["retained_count"] == 0
    assert result["metrics"]["model_all_positive_count"] == 0
    assert not result["metrics"]["all_retained_evaluated"]
    assert runner.replay_judgment(result["judgment_dir"])["exit_code"] == 0
    assert original == {name: (setup.source / name).read_bytes() for name in original}


def test_prepare_max_items_preserves_rows_without_any_client(setup):
    setup.source = make_source(setup.root / "two", count=2)
    def forbidden(settings):
        raise AssertionError("No client in prepare")
    result = runner.run_judge(setup.source, max_items=1, client_factory=forbidden)
    assert result["status"] == "prepared" and result["exit_code"] == 0
    assert len(rows(result)) == 2 and result["metrics"]["retained_count"] == 2
    assert all(row[phase]["reason"] == "prepare" for row in rows(result) for phase in runner.PHASES)
    assert runner.replay_judgment(result["judgment_dir"])["exit_code"] == 0


def test_each_run_has_new_directory_and_keeps_old_judgments(setup):
    first, _ = execute(setup)
    old = {p.name: p.read_bytes() for p in Path(first["judgment_dir"]).iterdir()}
    second, _ = execute(setup)
    assert first["judgment_dir"] != second["judgment_dir"]
    assert old == {name: Path(first["judgment_dir"], name).read_bytes() for name in old}


def test_client_initialization_failure_is_safe_and_creates_no_call_intent(setup):
    def fail_client(settings):
        raise RuntimeError("PRIVATE_JUDGE_KEY client init")
    with pytest.raises(runner.JudgeRunError) as error:
        execute(setup, client_factory=fail_client)
    assert "PRIVATE_JUDGE_KEY" not in str(error.value)
    directory = next((setup.source / "judgments").iterdir())
    assert (directory / "calls.jsonl").read_text() == ""
    assert not (directory / "complete.json").exists()


def test_missing_usage_is_unknown_not_zero(setup):
    answers = [response(body("answerability")), response(body("support"))]
    for answer in answers:
        answer.usage = None
    result, fake = execute(setup, Fake(answers))
    assert len(fake.calls) == 2 and result["status"] == "completed"
    assert all(value is None for value in result["metrics"]["usage"].values())
    assert result["metrics"]["cost"] is None


def test_consistency_conflict_preserves_raw_model_verdict_without_retry(setup):
    bad = body("support")
    bad["claims"].append({"text": "Explain the comparison.", "verdict": "unknown", "evidence": []})
    result, fake = execute(setup, Fake([response(body("answerability")), response(bad)]))
    assert len(fake.calls) == 2 and result["status"] == "completed"
    state = rows(result)[0]["support"]
    assert state["status"] == "evaluated" and "review" not in state
    assert state["model_review"] == bad
    assert json.loads(state["raw_response"]) == bad
    assert state["verification"]["structure"] == "valid"
    assert state["verification"]["consistency"] == "diagnostics"
    assert result["metrics"]["model_all_positive_count"] == 1
    assert "outcome_counts" not in result["metrics"]
    assert result["metrics"]["retry_count"] == 0
    replay = runner.replay_judgment(result["judgment_dir"])
    assert replay["exit_code"] == 0
    assert replay["metrics"]["human_review_pending_item_count"] == 1

    assert state["human_review"]["status"] == "pending"
    assert result["metrics"]["evaluated_phase_count"] == 2


def test_invalid_json_retains_raw_visible_output_but_no_model_review(setup):
    raw = "not JSON # raw visible response"
    result, fake = execute(setup, Fake([response(raw), response(raw), response(body("support"))]))
    state = rows(result)[0]["answerability"]
    assert len(fake.calls) == 3
    assert state["status"] == "error" and state["reason"] == "schema_error"
    assert state["raw_response"] == raw
    assert state["model_review"] is None and "review" not in state
    assert state["verification"]["structure"] == "invalid"
    assert state["technical_status"] == "error"
    assert state["verification"]["diagnostics"] == [{"code": "schema_invalid_json", "path": "$"}]
    assert runner.replay_judgment(result["judgment_dir"])["exit_code"] == 1


@pytest.mark.parametrize("answer,support,expected_pending", [
    ("not_answerable", "unsupported", 0), ("unknown", "unknown", 1),
])
def test_negative_and_uncertain_reviews_have_separate_human_routing(setup, answer, support, expected_pending):
    result, fake = execute(setup, Fake([response(body("answerability", answer)), response(body("support", support))]))
    assert result["status"] == "completed" and len(fake.calls) == 2
    assert result["metrics"]["evaluated_phase_count"] == 2
    assert result["metrics"]["human_review_pending_item_count"] == expected_pending
    assert result["metrics"]["technical_error_phase_count"] == 0
    assert result["metrics"]["model_all_positive_count"] == 0


def test_human_queue_persisted_hashed_and_replayed_without_altering_model(setup):
    value = body("support")
    value["claims"][0]["evidence"][0]["chunk_id"] = "unknown"
    result, fake = execute(setup, Fake([response(body("answerability")), response(value)]))
    directory = Path(result["judgment_dir"])
    queue = [json.loads(line) for line in (directory / "review_queue.jsonl").read_text().splitlines()]
    assert len(fake.calls) == 2 and result["status"] == "completed"
    assert queue[0]["model_review"] == value and queue[0]["phase"] == "support"
    assert queue[0]["status"] == "pending"
    assert any(reason["code"] == "unknown_chunk" for reason in queue[0]["reasons"])
    assert result["metrics"]["model_all_positive_count"] == 1
    assert result["metrics"]["human_review_pending_item_count"] == 1
    assert result["metrics"]["evaluated_phase_count"] == 2
    complete = json.loads((directory / "complete.json").read_text())
    assert complete["artifacts"]["review_queue.jsonl"] == runner.digest((directory / "review_queue.jsonl").read_bytes())
    replay = runner.replay_judgment(directory)
    assert Path(replay["replay_dir"], "review_queue.jsonl").read_bytes() == (directory / "review_queue.jsonl").read_bytes()
    assert replay["status"] == "completed"
