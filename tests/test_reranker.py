"""Provider-neutral reranker contract; every test is offline."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from overfit.errors import (
    RerankContractError,
    RerankError,
    RerankExternalError,
    RerankLogError,
)
from overfit.models import Chunk, RetrievedChunk
from overfit.query.rerankers.base import (
    RerankResponse,
    RerankResult,
    RerankScore,
    RerankStatus,
    SelectionDecision,
)
from overfit.query.rerankers.logging import RerankRunLogger, safe_endpoint_identity
from overfit.query.rerankers.service import RerankService


def _hit(index: int, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        Chunk(f"c{index}", f"document {index}", f"source-{index}.pdf", index + 1),
        vector_score=score,
        vector_rank=index + 1,
    )


@pytest.mark.parametrize("status", [RerankStatus.SKIPPED, RerankStatus.FAILED])
def test_success_is_derived_only_from_applied_state(status):
    result = RerankResult(
        status=status,
        ranked=(),
        selected=(),
        attempts=0,
        latency_ms=0.0,
        run_path=None,
    )

    assert result.success is False


@dataclass
class FakeReranker:
    provider: str = "fixture"
    model: str = "fixture-reranker"

    def __post_init__(self):
        self.calls = []

    def rerank(self, query, documents):
        self.calls.append((query, documents))
        return RerankResponse(
            scores=(
                RerankScore(index=1, score=0.9),
                RerankScore(index=0, score=0.4),
                RerankScore(index=2, score=0.2),
            ),
            request_id="fixture-request",
            usage={"total_tokens": 12, "debug": "SECRET-RESPONSE"},
        )


def test_service_preserves_dense_evidence_and_logs_selection(tmp_path):
    adapter = FakeReranker()
    service = RerankService(
        adapter,
        output_dir=tmp_path,
        timeout=30,
        sleep=lambda _: None,
    )
    candidates = [_hit(0, 0.8), _hit(1, 0.7), _hit(2, 0.6)]

    result = service.apply(
        "which document?",
        candidates,
        command="search",
        selector=lambda ranked: SelectionDecision(
            selected=tuple(ranked[:2]),
            drop_reasons={ranked[2].chunk.id: "outside_final_limit"},
        ),
    )

    assert result.status is RerankStatus.APPLIED and result.success
    assert [hit.chunk.id for hit in result.selected] == ["c1", "c0"]
    assert [hit.vector_score for hit in result.ranked] == [0.7, 0.8, 0.6]
    assert [hit.vector_rank for hit in result.ranked] == [2, 1, 3]
    assert [hit.rerank_score for hit in result.ranked] == [0.9, 0.4, 0.2]
    assert [hit.rerank_rank for hit in result.ranked] == [1, 2, 3]
    assert result.attempts == 1
    assert adapter.calls[0][0] == "which document?"
    assert [item.index for item in adapter.calls[0][1]] == [0, 1, 2]
    assert [item.text for item in adapter.calls[0][1]] == [
        "document 0",
        "document 1",
        "document 2",
    ]

    log_text = result.run_path.read_text()
    assert "fixture-request" in log_text
    assert "document 0" not in log_text
    assert "SECRET-RESPONSE" not in log_text
    assert "outside_final_limit" in log_text


def test_attempt_intent_is_persisted_before_provider_call(tmp_path):
    class InspectIntent(FakeReranker):
        def rerank(self, query, documents):
            record_path = next((tmp_path / "rerank" / "runs").glob("*.json"))
            record = json.loads(record_path.read_text())
            assert len(record["attempts"]) == 1
            assert record["attempts"][0]["number"] == 1
            assert record["attempts"][0]["outcome"] == "started"
            assert record["attempts"][0]["finished_at"] is None
            assert record["candidate_budget"] == 3
            assert record["actual_candidate_count"] == 3
            assert record["final_budget"] == 3
            assert record["selection_policy"]["applied"] is False
            return super().rerank(query, documents)

    service = RerankService(
        InspectIntent(), output_dir=tmp_path, sleep=lambda _: None
    )

    result = service.apply(
        "q",
        [_hit(0, 0.8), _hit(1, 0.7), _hit(2, 0.6)],
        command="search",
        selector=_all,
    )

    record = json.loads(result.run_path.read_text())
    assert len(record["attempts"]) == 1
    assert record["attempts"][0]["outcome"] == "success"
    assert record["attempts"][0]["finished_at"] is not None


def _all(ranked):
    return SelectionDecision(selected=tuple(ranked), drop_reasons={})


class ScriptedReranker(FakeReranker):
    def __init__(self, outcomes):
        super().__init__()
        self.outcomes = list(outcomes)

    def rerank(self, query, documents):
        self.calls.append((query, documents))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _response(*scores):
    return RerankResponse(
        scores=tuple(RerankScore(index=index, score=score) for index, score in enumerate(scores))
    )


def test_only_external_failure_retries_with_bounded_backoff(tmp_path):
    adapter = ScriptedReranker(
        [RerankExternalError(status=401), RerankExternalError(status=503), _response(0.5)]
    )
    waits = []
    service = RerankService(adapter, output_dir=tmp_path, sleep=waits.append)

    result = service.apply("q", [_hit(0, 0.4)], command="search", selector=_all)

    assert result.attempts == 3
    assert len(adapter.calls) == 3
    assert waits == [1.0, 2.0]


def test_three_external_failures_abort_and_are_recorded(tmp_path):
    adapter = ScriptedReranker([RerankExternalError()] * 3)
    service = RerankService(adapter, output_dir=tmp_path, sleep=lambda _: None)

    with pytest.raises(RerankError, match="three attempts"):
        service.apply("q", [_hit(0, 0.4)], command="search", selector=_all)

    record = next((tmp_path / "rerank" / "runs").glob("*.json")).read_text()
    assert '"status": "failed"' in record
    assert record.count("external_request_failed") == 4  # three attempts + terminal category


@pytest.mark.parametrize(
    "scores",
    [
        (RerankScore(0, 0.8),),
        (RerankScore(0, 0.8), RerankScore(0, 0.7)),
        (RerankScore(0, float("nan")), RerankScore(1, 0.7)),
        (RerankScore(0, 0.8), RerankScore(9, 0.7)),
    ],
)
def test_invalid_success_response_fails_once_and_records_other(tmp_path, scores):
    adapter = ScriptedReranker([RerankResponse(scores=scores)])
    service = RerankService(adapter, output_dir=tmp_path, sleep=lambda _: None)

    with pytest.raises(RerankContractError) as caught:
        service.apply(
            "q", [_hit(0, 0.4), _hit(1, 0.3)], command="search", selector=_all
        )

    assert len(adapter.calls) == 1
    assert caught.value.attempts == 1
    record = json.loads(
        next((tmp_path / "rerank" / "runs").glob("*.json")).read_text()
    )
    assert record["status"] == "failed"
    assert record["failure_category"] == "other"
    assert record["attempts"][0]["outcome"] == "other"


def test_shutdown_propagates_without_retry(tmp_path):
    adapter = ScriptedReranker([KeyboardInterrupt()])
    service = RerankService(adapter, output_dir=tmp_path, sleep=lambda _: None)

    with pytest.raises(KeyboardInterrupt):
        service.apply("q", [_hit(0, 0.4)], command="search", selector=_all)

    assert len(adapter.calls) == 1
    record = next((tmp_path / "rerank" / "runs").glob("*.json")).read_text()
    assert '"failure_category": "shutdown"' in record


def test_no_candidates_calls_no_provider_and_records_failure(tmp_path):
    adapter = ScriptedReranker([])
    service = RerankService(adapter, output_dir=tmp_path, sleep=lambda _: None)

    with pytest.raises(RerankContractError, match="No candidates"):
        service.apply("q", [], command="search", selector=_all)

    assert adapter.calls == []
    record = next((tmp_path / "rerank" / "runs").glob("*.json")).read_text()
    assert '"failure_category": "no_candidates"' in record


def test_initial_log_failure_calls_no_provider(tmp_path, monkeypatch):
    adapter = ScriptedReranker([_response(0.5)])

    def fail(self):
        raise RerankLogError("fixture")

    monkeypatch.setattr(RerankRunLogger, "_persist", fail)
    service = RerankService(adapter, output_dir=tmp_path, sleep=lambda _: None)

    with pytest.raises(RerankLogError):
        service.apply("q", [_hit(0, 0.4)], command="search", selector=_all)
    assert adapter.calls == []


def test_attempt_intent_log_failure_calls_no_provider(tmp_path, monkeypatch):
    adapter = ScriptedReranker([_response(0.5)])

    def fail(self, *args, **kwargs):
        raise RerankLogError("fixture")

    monkeypatch.setattr(RerankRunLogger, "start_attempt", fail)
    service = RerankService(adapter, output_dir=tmp_path, sleep=lambda _: None)

    with pytest.raises(RerankLogError) as caught:
        service.apply("q", [_hit(0, 0.4)], command="search", selector=_all)
    assert caught.value.attempts == 0
    assert adapter.calls == []


def test_attempt_completion_log_failure_reports_sent_call(tmp_path, monkeypatch):
    adapter = ScriptedReranker([_response(0.5)])

    def fail(self, *args, **kwargs):
        raise RerankLogError("fixture")

    monkeypatch.setattr(RerankRunLogger, "finish_attempt", fail)
    service = RerankService(adapter, output_dir=tmp_path, sleep=lambda _: None)

    with pytest.raises(RerankLogError) as caught:
        service.apply("q", [_hit(0, 0.4)], command="search", selector=_all)
    assert caught.value.attempts == 1
    assert len(adapter.calls) == 1


def test_post_response_log_failure_stops_after_one_provider_call(tmp_path, monkeypatch):
    adapter = ScriptedReranker([_response(0.5)])

    def fail(self, *args, **kwargs):
        raise RerankLogError("fixture")

    monkeypatch.setattr(RerankRunLogger, "finish_success", fail)
    service = RerankService(adapter, output_dir=tmp_path, sleep=lambda _: None)

    with pytest.raises(RerankLogError) as caught:
        service.apply("q", [_hit(0, 0.4)], command="search", selector=_all)
    assert len(adapter.calls) == 1
    assert caught.value.attempts == 1


def test_endpoint_identity_removes_credentials_query_and_fragment():
    assert safe_endpoint_identity(
        "https://user:SECRET@example.com/api/v1?token=SECRET#fragment"
    ) == "https://example.com/api/v1"

