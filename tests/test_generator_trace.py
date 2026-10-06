"""Offline generation trace contracts: fake clients only, no settings files."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from overfit.config import LLMSettings
from overfit.evaluation.contracts import TraceWriteError
from overfit.models import Chunk, GeneratedExam
from overfit.query import generator as module
from overfit.query.generator import GenerationError, Generator, _drop_invented_citations


class Recorder:
    def __init__(self, fail_on=None):
        self.events = []
        self.last_call_id = None
        self.fail_on = fail_on

    def emit(self, event_type, payload):
        if self.fail_on == event_type:
            raise TraceWriteError("synthetic write failure")
        self.events.append((event_type, deepcopy(payload)))

    def start_call(self, attempt_index, format_index, request):
        call_id = f"call-{1 + sum(name == 'call_started' for name, _ in self.events)}"
        self.emit("call_started", {
            "call_id": call_id, "attempt_index": attempt_index,
            "format_index": format_index, "request": request,
        })
        self.last_call_id = call_id
        return call_id

    def of(self, event_type):
        return [payload for name, payload in self.events if name == event_type]


class Client:
    def __init__(self, responses, recorder=None):
        self.responses = iter(responses)
        self.calls = []
        self.recorder = recorder
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        if self.recorder is not None:
            assert self.recorder.events[-1][0] == "call_started"
        self.calls.append(deepcopy(kwargs))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, str):
            return iter([part(response)])
        return response


def part(content="", reasoning=None, usage=None, choices=True):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta)] if choices else [], usage=usage,
    )


def item(page=5, source="notes.pdf"):
    return {"topic": "Topic", "question": "Q?", "answer": "A", "page": page, "source": source}


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    # Even accidental future settings reads have no project .env to discover.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        temperature=0.25, request_timeout=123.0, max_retries=2,
    ))


def generator(responses, recorder=None):
    instance = Generator(LLMSettings(_env_file=None, model="fixture-model", api_key="fixture"))
    instance._client = Client(responses, recorder)
    return instance


def complete(instance, recorder=None, attempts=2):
    return instance._complete("system", "user", GeneratedExam, attempts, recorder=recorder)


@pytest.mark.parametrize("traced", [False, True])
def test_fallback_ladder_and_attempts_unchanged(traced):
    recorder = Recorder() if traced else None
    instance = generator([
        RuntimeError("unsupported schema"), RuntimeError("unsupported JSON"), "{}",
        RuntimeError("unsupported JSON"), '{"items": []}',
    ], recorder)
    exam, attempts, raw = complete(instance, recorder)
    assert exam.items == [] and attempts == 2 and raw == '{"items": []}'
    calls = instance._client.calls
    assert [call["response_format"]["type"] if call["response_format"] else None
            for call in calls] == ["json_schema", "json_object", None, "json_object", None]
    assert len(calls) == 5
    assert ("timeout" in calls[0]) is traced
    assert calls[-1]["messages"][2]["content"] == "{}"
    assert "did not match the schema" in calls[-1]["messages"][3]["content"]
    if recorder:
        starts = recorder.of("call_started")
        assert [(e["attempt_index"], e["format_index"]) for e in starts] == [
            (1, 1), (1, 2), (1, 3), (2, 1), (2, 2),
        ]
        assert len({event["call_id"] for event in starts}) == 5
        assert [event["request"] for event in starts] == calls
        assert len(starts[0]["request"]["messages"]) == 2
        assert [event["status"] for event in recorder.of("validation_finished")] == ["invalid", "valid"]
        assert all(e["elapsed_seconds"] >= 0 for e in recorder.of("call_finished"))


def test_empty_body_distinct_from_valid_empty_items():
    recorder = Recorder()
    instance = generator([" \n", '{"items": []}'], recorder)
    assert complete(instance, recorder)[0].items == []
    validations = recorder.of("validation_finished")
    assert [(e["status"], e["item_count"]) for e in validations] == [
        ("empty_body", None), ("valid", 0),
    ]


@pytest.mark.parametrize("raw", ["not JSON", '{"items": [{"question": "missing fields"}]}'])
def test_invalid_output_and_exhaustion_preserve_visible_response(raw):
    recorder = Recorder()
    instance = generator([raw], recorder)
    with pytest.raises(GenerationError):
        complete(instance, recorder, attempts=1)
    assert len(instance._client.calls) == 1
    assert recorder.of("call_finished")[0]["content"] == raw
    assert recorder.of("validation_finished")[0]["status"] == "invalid"


def broken_stream(exc):
    yield part("visible fragment", reasoning="SECRET_REASONING")
    raise exc


@pytest.mark.parametrize("exception", [RuntimeError("SECRET_PROVIDER_KEY"), KeyboardInterrupt()])
def test_partial_stream_error_or_interrupt(exception):
    recorder = Recorder()
    instance = generator([broken_stream(exception), '{"items": []}'], recorder)
    if isinstance(exception, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            complete(instance, recorder)
        assert len(instance._client.calls) == 1
    else:
        complete(instance, recorder)
        assert len(instance._client.calls) == 2
    event = recorder.of("call_finished")[0]
    assert event["content"] == "visible fragment"
    assert event["status"] == "error" and event["response_complete"] is False
    serialized = json.dumps(recorder.events)
    assert "SECRET_PROVIDER_KEY" not in serialized and "SECRET_REASONING" not in serialized


def test_reasoning_is_only_progress_and_usage_is_allowlisted():
    recorder = Recorder()
    stream = iter([
        part(reasoning="SECRET_REASONING"),
        part('{"items": []}'),
        part(choices=False, usage=SimpleNamespace(
            prompt_tokens=3, completion_tokens=4, total_tokens=7,
            private="SECRET_METADATA",
        )),
    ])
    instance = generator([stream], recorder)
    progress = []
    instance._complete("s", "u", GeneratedExam, 1,
                       on_token=lambda *args: progress.append(args), recorder=recorder)
    assert progress == [(0, 1), (1, 1)]
    event = recorder.of("call_finished")[0]
    assert event["usage"] == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    assert event["response_complete"] is True
    assert "SECRET" not in json.dumps(recorder.events)
    assert "stream_options" not in instance._client.calls[0]


@pytest.mark.parametrize("fail_on,expected_calls", [
    ("call_started", 0), ("call_finished", 1), ("validation_finished", 1),
])
def test_trace_write_failure_never_falls_back(fail_on, expected_calls):
    recorder = Recorder(fail_on=fail_on)
    instance = generator(['{"items": []}'] * 6, recorder)
    with pytest.raises(TraceWriteError):
        complete(instance, recorder, attempts=3)
    assert len(instance._client.calls) == expected_calls


def test_error_record_write_failure_never_falls_back():
    recorder = Recorder(fail_on="call_finished")
    instance = generator([RuntimeError("failure"), '{"items": []}'], recorder)
    with pytest.raises(TraceWriteError):
        complete(instance, recorder)
    assert len(instance._client.calls) == 1


def test_citation_mapping_repair_drop_duplicates_and_policy_parity():
    chunks = [Chunk("c1", "content", "notes.pdf", 5, 6)]
    data = {"items": [item(), item(), item(4), item(20), item(source="unknown.pdf")]}
    old, old_dropped = _drop_invented_citations(GeneratedExam.model_validate(data), chunks)
    recorder = Recorder()
    recorder.last_call_id = "call-1"
    new, new_dropped = _drop_invented_citations(
        GeneratedExam.model_validate(data), chunks, recorder=recorder,
    )
    assert new == old and new_dropped == old_dropped
    events = recorder.of("citation_processed")
    assert [event["action"] for event in events] == ["keep", "keep", "repair", "drop", "drop"]
    assert [event["final_index"] for event in events] == [1, 2, 3, None, None]
    assert [event["item_id"] for event in events] == [f"call-1-item-{i}" for i in range(1, 6)]
    assert events[2]["pre_policy"]["page"] == 4
    assert events[2]["post_policy"]["page"] == 5
    assert events[2]["pre_check"] != events[2]["post_check"]
    assert events[3]["post_check"] is None and events[3]["post_policy"] is None


def test_equal_distance_tie_break_matches_original_set_iteration():
    chunks = [Chunk("c1", "a", "notes.pdf", 4), Chunk("c2", "b", "notes.pdf", 6)]
    recorder = Recorder()
    recorder.last_call_id = "call-1"
    exam = GeneratedExam(items=[item(5)])
    actual, _ = _drop_invented_citations(exam, chunks, recorder=recorder)
    expected = min({4, 6}, key=lambda p: abs(p - 5))
    assert actual.items[0].page == expected


def test_mock_exam_public_hook_records_material_prompt_and_citation():
    chunks = [Chunk("chunk-2", "SECOND", "notes.pdf", 5), Chunk("chunk-1", "FIRST", "notes.pdf", 8)]
    recorder = Recorder()
    instance = generator([json.dumps({"items": [item()]})], recorder)
    result = instance.mock_exam("COURSE", chunks, count=1, max_attempts=1, recorder=recorder)
    assert len(result.exam.items) == 1 and result.dropped == []
    request = recorder.of("call_started")[0]["request"]
    assert request == instance._client.calls[0]
    assert request["model"] == "fixture-model" and request["temperature"] == 0.25
    assert request["timeout"] == 123.0
    user = request["messages"][1]["content"]
    assert user.index("SECOND") < user.index("FIRST")
    assert recorder.of("citation_processed")[0]["call_id"] == recorder.last_call_id


def test_citation_write_failure_propagates_after_only_one_call():
    recorder = Recorder(fail_on="citation_processed")
    instance = generator([json.dumps({"items": [item()]})], recorder)
    with pytest.raises(TraceWriteError):
        instance.mock_exam("COURSE", [Chunk("c1", "text", "notes.pdf", 5)],
                           count=1, recorder=recorder)
    assert len(instance._client.calls) == 1


def test_all_provider_formats_exhausted_have_error_events_and_no_validation():
    recorder = Recorder()
    instance = generator([TimeoutError("SECRET") for _ in range(5)], recorder)
    with pytest.raises(GenerationError, match="after 2 attempts"):
        complete(instance, recorder)
    assert len(instance._client.calls) == 5
    assert len(recorder.of("call_finished")) == 5
    assert not recorder.of("validation_finished")
    for event in recorder.of("call_finished"):
        assert event["status"] == "error" and event["content"] == ""
        assert event["usage"] is None and event["response_complete"] is False
    assert "SECRET" not in json.dumps(recorder.events)


def test_trace_and_untraced_results_match_when_every_item_is_dropped():
    raw = json.dumps({"items": [item(30), item(source="unknown.pdf")]})
    chunks = [Chunk("c1", "content", "notes.pdf", 5)]
    recorder = Recorder()
    traced = generator([raw], recorder).mock_exam(
        "COURSE", chunks, count=2, max_attempts=1, recorder=recorder,
    )
    untraced = generator([raw]).mock_exam("COURSE", chunks, count=2, max_attempts=1)
    assert traced == untraced
    assert traced.exam.items == [] and len(traced.dropped) == 2
    assert recorder.of("validation_finished")[0]["item_count"] == 2
    assert all(e["action"] == "drop" for e in recorder.of("citation_processed"))


def test_normalized_pre_policy_is_not_claimed_to_be_raw_response():
    raw = json.dumps({"items": [item("5")]})
    recorder = Recorder()
    instance = generator([raw], recorder)
    instance.mock_exam("COURSE", [Chunk("c1", "text", "notes.pdf", 5)],
                       count=1, recorder=recorder)
    assert json.loads(recorder.of("call_finished")[0]["content"])["items"][0]["page"] == "5"
    assert recorder.of("citation_processed")[0]["pre_policy"]["page"] == 5
