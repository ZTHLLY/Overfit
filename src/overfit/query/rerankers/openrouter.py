"""The first and only v1 provider adapter: OpenRouter's rerank endpoint."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from overfit.errors import (
    RerankConfigurationError,
    RerankContractError,
    RerankExternalError,
)
from overfit.query.rerankers.base import (
    RerankDocument,
    RerankResponse,
    RerankScore,
)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


Transport = Callable[[str, Mapping[str, str], bytes, float], HttpResponse]


class OpenRouterReranker:
    provider = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "voyageai/rerank-3",
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        if not api_key.strip():
            raise RerankConfigurationError(
                "A reranker API key is required for query commands."
            )
        # Other OpenRouter rerank slugs can be enabled by configuration
        # without another adapter; an empty value is never allowed.
        if not model.strip():
            raise RerankConfigurationError("Reranker model must not be empty.")
        if not base_url.startswith("https://"):
            raise RerankConfigurationError("Reranker base URL must use HTTPS.")
        if timeout <= 0:
            raise RerankConfigurationError("Reranker timeout must be positive.")
        self._api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport or _urllib_transport

    def rerank(
        self, query: str, documents: Sequence[RerankDocument]
    ) -> RerankResponse:
        payload = {
            "model": self.model,
            "query": query,
            "documents": [item.text for item in documents],
            "top_n": len(documents),
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self._transport(
                f"{self.base_url}/rerank",
                headers,
                json.dumps(payload, ensure_ascii=False).encode(),
                self.timeout,
            )
        except KeyboardInterrupt:
            raise
        except (TimeoutError, URLError, ConnectionError, OSError):
            raise RerankExternalError() from None
        if response.status < 200 or response.status >= 300:
            raise RerankExternalError(
                status=response.status,
                request_id=_header(response.headers, "x-request-id"),
                retry_after=_retry_after(response.headers),
            )
        try:
            data = json.loads(response.body)
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise TypeError
            scores = []
            for item in data["results"]:
                if not isinstance(item, dict):
                    raise TypeError
                index = item["index"]
                score = item["relevance_score"]
                scores.append(RerankScore(index=index, score=score))
            request_id = data.get("id")
            provider = data.get("provider")
            usage = data.get("usage")
            if request_id is not None and not isinstance(request_id, str):
                raise ValueError
            if provider is not None and not isinstance(provider, str):
                raise ValueError
            if usage is not None and not isinstance(usage, dict):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise RerankContractError(
                "OpenRouter returned an invalid rerank response."
            ) from None
        return RerankResponse(
            scores=tuple(scores),
            request_id=request_id,
            provider=provider,
            usage=usage,
        )


def _urllib_transport(
    url: str, headers: Mapping[str, str], body: bytes, timeout: float
) -> HttpResponse:
    request = Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return HttpResponse(
                status=response.status,
                body=response.read(),
                headers=dict(response.headers.items()),
            )
    except HTTPError as exc:
        headers = dict(exc.headers.items()) if exc.headers is not None else {}
        return HttpResponse(status=exc.code, body=b"", headers=headers)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = next(
        (item for key, item in headers.items() if key.casefold() == name.casefold()),
        None,
    )
    return value if isinstance(value, str) else None


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = _header(headers, "retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None
