"""Reranker orchestration: bounded retries, validation, selection and audit."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from overfit.errors import RerankContractError, RerankError, RerankExternalError
from overfit.models import RetrievedChunk
from overfit.query.rerankers.base import (
    RerankDocument,
    Reranker,
    RerankResult,
    RerankScore,
    RerankStatus,
    SelectionDecision,
)
from overfit.query.rerankers.logging import RerankRunLogger


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class RerankService:
    def __init__(
        self,
        adapter: Reranker,
        *,
        output_dir: Path,
        timeout: float = 30.0,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts != 3:
            raise RerankContractError("Rerank max attempts must be three.")
        self.adapter = adapter
        self.output_dir = Path(output_dir)
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.clock = clock

    def apply(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        *,
        command: str,
        selector: Callable[[list[RetrievedChunk]], SelectionDecision],
        pre_dropped: Sequence[tuple[RetrievedChunk, str]] = (),
        candidate_budget: int | None = None,
        final_budget: int | None = None,
        near_duplicate_threshold: float | None = None,
        source_cap: int | None = None,
    ) -> RerankResult:
        if not query.strip():
            raise RerankContractError("A rerank query must not be empty.")
        candidate_budget = (
            len(candidates) if candidate_budget is None else candidate_budget
        )
        final_budget = len(candidates) if final_budget is None else final_budget
        if candidate_budget < len(candidates) or final_budget < 0:
            raise RerankContractError("Rerank budgets do not match the candidate set.")
        documents = tuple(
            RerankDocument(index=index, text=item.chunk.text)
            for index, item in enumerate(candidates)
        )
        logger = RerankRunLogger.create(
            self.output_dir,
            command=command,
            query=query,
            candidates=list(candidates),
            pre_dropped=list(pre_dropped),
            provider=self.adapter.provider,
            model=self.adapter.model,
            timeout=self.timeout,
            candidate_budget=candidate_budget,
            final_budget=final_budget,
            near_duplicate_threshold=near_duplicate_threshold,
            source_cap=source_cap,
            endpoint=getattr(self.adapter, "base_url", None),
        )
        run_started = self.clock()
        if not candidates:
            logger.finish_failure(outcome="no_candidates", latency_ms=0.0)
            raise RerankContractError("No candidates are available for reranking.")
        response = None
        ranked = None
        attempts = 0
        for attempt in range(1, self.max_attempts + 1):
            started_at = _now()
            logger.start_attempt(attempt, started_at=started_at)
            started = self.clock()
            # The intent is durable before this counter advances: attempts is
            # the number of provider calls actually sent, not loop iterations.
            attempts = attempt
            try:
                response = self.adapter.rerank(query, documents)
                ranked = rank_response(candidates, response.scores)
            except KeyboardInterrupt:
                logger.finish_attempt(
                    attempt,
                    finished_at=_now(),
                    latency_ms=(self.clock() - started) * 1000,
                    outcome="shutdown",
                )
                logger.finish_failure(
                    outcome="shutdown",
                    latency_ms=(self.clock() - run_started) * 1000,
                )
                raise
            except RerankExternalError as exc:
                try:
                    logger.finish_attempt(
                        attempt,
                        finished_at=_now(),
                        latency_ms=(self.clock() - started) * 1000,
                        outcome="external_request_failed",
                        status=exc.status,
                        request_id=exc.request_id,
                    )
                except RerankError as log_exc:
                    log_exc.attempts = attempts
                    raise
                if attempt == self.max_attempts:
                    try:
                        logger.finish_failure(
                            outcome="external_request_failed",
                            latency_ms=(self.clock() - run_started) * 1000,
                        )
                    except RerankError as log_exc:
                        log_exc.attempts = attempts
                        raise
                    raise RerankError(
                        "Reranker failed after three attempts; generation aborted.",
                        attempts=3,
                        failure_category="external_or_timeout",
                    ) from None
                default_delay = float(attempt)
                delay = (
                    exc.retry_after
                    if exc.retry_after is not None and exc.retry_after <= default_delay
                    else default_delay
                )
                self.sleep(delay)
                continue
            except Exception as exc:
                if isinstance(exc, RerankError):
                    exc.attempts = attempts
                try:
                    logger.finish_attempt(
                        attempt,
                        finished_at=_now(),
                        latency_ms=(self.clock() - started) * 1000,
                        outcome="other",
                    )
                    logger.finish_failure(
                        outcome="other",
                        latency_ms=(self.clock() - run_started) * 1000,
                    )
                except RerankError as log_exc:
                    log_exc.attempts = attempts
                    raise
                raise
            try:
                logger.finish_attempt(
                    attempt,
                    finished_at=_now(),
                    latency_ms=(self.clock() - started) * 1000,
                    outcome="success",
                    request_id=response.request_id,
                )
            except RerankError as log_exc:
                log_exc.attempts = attempts
                raise
            break
        assert response is not None and ranked is not None
        try:
            decision = selector(ranked)
            _validate_selection(ranked, decision)
        except Exception as exc:
            if isinstance(exc, RerankError):
                exc.attempts = attempts
            try:
                logger.finish_failure(
                    outcome="other",
                    latency_ms=(self.clock() - run_started) * 1000,
                )
            except RerankError as log_exc:
                log_exc.attempts = attempts
                raise
            raise
        latency_ms = (self.clock() - run_started) * 1000
        try:
            logger.finish_success(
                ranked,
                decision.selected,
                decision.drop_reasons,
                latency_ms=latency_ms,
                response_id=response.request_id,
                provider=response.provider,
                usage=response.usage,
            )
        except RerankError as log_exc:
            log_exc.attempts = attempts
            raise
        return RerankResult(
            status=RerankStatus.APPLIED,
            ranked=tuple(ranked),
            selected=decision.selected,
            attempts=attempts,
            latency_ms=latency_ms,
            run_path=logger.run_path,
            provider=self.adapter.provider,
            model=self.adapter.model,
            response=response,
        )


def rank_response(
    candidates: Sequence[RetrievedChunk], scores: Sequence[RerankScore]
) -> list[RetrievedChunk]:
    """Validate a complete provider response and preserve dense evidence."""
    expected = set(range(len(candidates)))
    seen = set()
    by_index = {}
    for result in scores:
        if type(result.index) is not int or result.index not in expected or result.index in seen:
            raise RerankContractError("Reranker returned invalid candidate indices.")
        if isinstance(result.score, bool) or not isinstance(result.score, (int, float)) or not math.isfinite(result.score):
            raise RerankContractError("Reranker returned a non-finite score.")
        seen.add(result.index)
        by_index[result.index] = float(result.score)
    if seen != expected:
        raise RerankContractError("Reranker returned an incomplete result set.")
    ordered = sorted(
        range(len(candidates)),
        key=lambda index: (-by_index[index], candidates[index].vector_rank),
    )
    return [
        candidates[index].with_rerank(by_index[index], rank)
        for rank, index in enumerate(ordered, 1)
    ]


def _validate_selection(
    ranked: Sequence[RetrievedChunk], decision: SelectionDecision
) -> None:
    known = {item.chunk.id for item in ranked}
    chosen = [item.chunk.id for item in decision.selected]
    if len(chosen) != len(set(chosen)) or not set(chosen).issubset(known):
        raise RerankContractError("Selection returned invalid candidates.")
    unselected = known - set(chosen)
    if set(decision.drop_reasons) != unselected:
        raise RerankContractError("Every unselected candidate needs one drop reason.")
