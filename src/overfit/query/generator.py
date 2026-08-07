"""Layer 7 -- Generator.

Assembles a prompt from retrieved material, calls the language model under a
JSON schema, validates what comes back, and renders Markdown.

Three things here are load-bearing:

**The schema is the grounding mechanism.** Requiring `source` and `page` on
every item forces the model to point at something it was given. That is an
enforceable constraint in a way that "please do not make things up" in a
prompt is not -- and the citation it produces is checkable by a human, which
is the actual guarantee this tool offers.

**Validation with retry, not hope.** A 27B model running locally follows a
schema most of the time, not always. When it does not, the failure is fed
back to it and the request is repeated. Without this the command fails on a
missing field after a minute of generation, which is the most annoying
possible outcome.

**Graceful degradation on structured output.** Not every endpoint supports
schema-constrained decoding. We ask for it, fall back to plain JSON mode, and
in the last resort parse JSON out of prose -- validating identically in all
three cases, so a weaker server means a slower path, not a broken one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

from jinja2 import Environment, PackageLoader, StrictUndefined
from pydantic import ValidationError

from overfit.config import LLMSettings, get_settings
from overfit.errors import OverfitError
from overfit.models import Chunk, GeneratedExam, GeneratedItem

__all__ = ["Generator", "GenerationResult", "GenerationError"]


class GenerationError(OverfitError):
    """The model never produced output matching the schema."""


@dataclass
class GenerationResult:
    exam: GeneratedExam
    attempts: int
    dropped: list[str]  # items rejected because their citation was invented


@lru_cache(maxsize=1)
def _templates() -> Environment:
    return Environment(
        loader=PackageLoader("overfit", "templates"),
        undefined=StrictUndefined,  # a typo in a template should fail loudly
        trim_blocks=True,
        lstrip_blocks=True,
    )


class Generator:
    def __init__(self, settings: LLMSettings | None = None) -> None:
        self._settings = settings or get_settings().llm
        self._client = None

    @property
    def model(self) -> str:
        return self._settings.model

    def _ensure_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                base_url=self._settings.base_url,
                api_key=self._settings.api_key,
                max_retries=0,
                timeout=get_settings().request_timeout,
            )
        return self._client

    # -- public API --------------------------------------------------------

    def mock_exam(
        self,
        course: str,
        chunks: list[Chunk],
        *,
        count: int,
        topic: str | None = None,
        max_attempts: int | None = None,
        on_token=None,
    ) -> GenerationResult:
        """Write practice questions from `chunks`."""
        system = _templates().get_template("system.j2").render()
        user = (
            _templates()
            .get_template("mock_prompt.j2")
            .render(course=course, chunks=chunks, count=count, topic=topic)
        )

        exam, attempts = self._complete(
            system,
            user,
            GeneratedExam,
            max_attempts or get_settings().max_retries + 1,
            on_token=on_token,
        )
        exam, dropped = _drop_invented_citations(exam, chunks)
        return GenerationResult(exam=exam, attempts=attempts, dropped=dropped)

    # -- internals ---------------------------------------------------------

    def _stream(self, client, response_format, messages, on_token) -> str:
        """Collect a streamed reply, reporting progress as it arrives.

        Streaming is not an optimisation here, it is the difference between a
        usable tool and one that looks broken. A large local model spends its
        first minute loading weights and its next two generating; without any
        sign of life a user cannot tell a slow run from a hung one, and will
        reasonably kill it. Tokens appearing is the only honest evidence that
        something is happening.
        """
        chunks: list[str] = []
        stream = client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=get_settings().temperature,
            response_format=response_format,
            stream=True,
        )
        thinking = 0
        for part in stream:
            if not part.choices:
                continue
            delta = part.choices[0].delta

            # Reasoning models emit a long internal monologue before any
            # answer, and it arrives on a different field. Counting only
            # `content` makes a model that is working flat out look frozen --
            # which is indistinguishable, from the outside, from a hang.
            reasoning = getattr(delta, "reasoning_content", None) or getattr(
                delta, "reasoning", None
            )
            if reasoning:
                thinking += 1
                if on_token:
                    on_token(len(chunks), thinking)

            piece = delta.content or ""
            if piece:
                chunks.append(piece)
                if on_token:
                    on_token(len(chunks), thinking)
        return "".join(chunks)

    def _complete(self, system: str, user: str, schema, max_attempts: int, on_token=None):
        """Call the model until the reply validates, or give up with detail."""
        client = self._ensure_client()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        json_schema = schema.model_json_schema()
        problems: list[str] = []

        for attempt in range(1, max_attempts + 1):
            # Strongest constraint the endpoint will accept, weakening as we
            # learn what it rejects.
            raw = None
            for response_format in _response_formats(json_schema, attempt):
                try:
                    raw = self._stream(client, response_format, messages, on_token)
                    break
                except Exception as exc:  # noqa: BLE001 - endpoint capability probe
                    problems.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
            if raw is None:
                continue
            try:
                return schema.model_validate(_extract_json(raw)), attempt
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                problems.append(f"attempt {attempt}: {_summarise(exc)}")
                # Show the model its own mistake. Local models correct a named
                # missing field far more reliably than a repeated instruction.
                messages = messages[:2] + [
                    {"role": "assistant", "content": raw[:2000]},
                    {
                        "role": "user",
                        "content": (
                            f"That response did not match the schema: "
                            f"{_summarise(exc)}\n"
                            f"Reply again with valid JSON only, no commentary."
                        ),
                    },
                ]

        raise GenerationError(
            f"{self.model} did not return valid output after {max_attempts} "
            f"attempts.\n  "
            + "\n  ".join(problems[-4:])
            + self._advice(problems)
        )

    def _advice(self, problems: list[str]) -> str:
        """Turn a low-level failure into something the user can act on.

        A timeout against a local endpoint almost never means the network;
        it means the machine could not hold the model and started swapping.
        Reporting it as a timeout sends people to look in the wrong place,
        so name the likely cause and the two cheapest ways out.
        """
        combined = " ".join(problems).lower()

        if "timeout" in combined or "timed out" in combined:
            return (
                f"\n\nA timeout on a local model usually means memory, not the "
                f"network: a 27B model needs roughly 17 GB, and anything else "
                f"already loaded competes with it.\n"
                f"  ollama ps                 # see what is resident\n"
                f"  ollama stop <model>       # free the embedding model\n"
                f"Or take a smaller step -- the provider is configuration, not "
                f"code:\n"
                f"  LLM_MODEL=qwen3:8b overfit mock ...\n"
                f"  --material 6              # shorter prompt, less context\n"
            )
        if "connect" in combined or "refused" in combined:
            return (
                f"\n\nNothing answered at {self._settings.base_url}. "
                f"If this is Ollama, check it is running: curl "
                f"{self._settings.base_url.rstrip('/v1')}"
            )
        return (
            "\n\nIf the model keeps producing malformed JSON, it may be too "
            "small to follow the schema. Try a larger local model, or point "
            "LLM_BASE_URL at a hosted one -- the rest of the pipeline is "
            "unaffected."
        )


def _response_formats(json_schema: dict, attempt: int):
    """Constraint options, strongest first.

    Schema-constrained decoding makes an invalid reply impossible rather than
    unlikely, so it is always worth asking for. Older or lighter servers
    reject the parameter outright, hence the ladder.
    """
    if attempt == 1:
        yield {
            "type": "json_schema",
            "json_schema": {"name": "exam", "schema": json_schema, "strict": True},
        }
    yield {"type": "json_object"}
    yield None  # unconstrained; _extract_json will dig the object out


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw: str) -> dict:
    """Parse the reply, tolerating the wrappers models like to add.

    Fenced code blocks and a sentence of preamble are common even when JSON
    was explicitly requested. Refusing to handle that would fail a response
    that is, apart from decoration, entirely correct.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(text)
        if not match:
            raise ValueError(f"no JSON object in reply: {text[:200]!r}") from None
        return json.loads(match.group())


def _drop_invented_citations(
    exam: GeneratedExam, chunks: list[Chunk]
) -> tuple[GeneratedExam, list[str]]:
    """Remove any item citing material that was not supplied.

    The schema guarantees a citation exists; it cannot guarantee the citation
    is real. Checking each one against the passages actually handed over
    turns the promise from "the model was told to cite" into "the citation
    refers to something", which is the only version worth printing.

    A near miss on the page number is repaired rather than discarded: models
    routinely cite the first page of a chunk that spans two, and throwing the
    question away over that would lose good work.
    """
    allowed: dict[str, set[int]] = {}
    for chunk in chunks:
        pages = allowed.setdefault(chunk.source, set())
        pages.add(chunk.page)
        if chunk.page_end:
            pages.update(range(chunk.page, chunk.page_end + 1))

    kept: list[GeneratedItem] = []
    dropped: list[str] = []
    for item in exam.items:
        if item.source not in allowed:
            dropped.append(f"{item.source} p{item.page} — no such file in the material")
            continue
        if item.page not in allowed[item.source]:
            nearest = min(allowed[item.source], key=lambda p: abs(p - item.page))
            if abs(nearest - item.page) > 2:
                dropped.append(
                    f"{item.source} p{item.page} — page not among those supplied"
                )
                continue
            item = item.model_copy(update={"page": nearest})
        kept.append(item)

    return GeneratedExam(items=kept), dropped


def render_exam(course: str, exam: GeneratedExam) -> tuple[str, str]:
    """Render the question paper and the answer key."""
    numbered = [
        {
            "number": index,
            "topic": item.topic.strip() or "General",
            "question": item.question.strip(),
            "answer": item.answer.strip(),
            "source": item.source,
            "page": item.page,
        }
        for index, item in enumerate(exam.items, start=1)
    ]

    grouped: dict[str, list[dict]] = {}
    for item in numbered:
        grouped.setdefault(item["topic"], []).append(item)

    context = {
        "course": course,
        "items": numbered,
        "grouped": list(grouped.items()),
        "sources": sorted({item["source"] for item in numbered}),
        "generated_at": date.today().isoformat(),
        "answers_file": f"{course}_answers.md",
    }
    env = _templates()
    return (
        env.get_template("mock_exam.md.j2").render(**context),
        env.get_template("mock_answers.md.j2").render(**context),
    )


def _summarise(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        return f"{location}: {first['msg']}"
    return str(exc)[:200]
