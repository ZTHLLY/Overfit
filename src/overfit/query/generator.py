"""Layer 7 -- Generator.

Assembles a prompt from retrieved material, asks the language model for a JSON
object, validates what comes back against the internal schema, and renders
Markdown.

Three things here are load-bearing:

**The schema is the grounding mechanism.** Requiring `source` and `page` on
every accepted item rejects output that does not point at something it was
given. That is an enforceable constraint in a way that "please do not make
things up" in a prompt is not -- and the citation it produces is checkable by
a human, which is the actual guarantee this tool offers.

**Validation with retry, not hope.** A 27B model running locally follows a
schema most of the time, not always. When it does not, the failure is fed
back to it and the request is repeated. Without this the command fails on a
missing field after a minute of generation, which is the most annoying
possible outcome.

**Portable structured output.** OpenAI-compatible endpoints do not all support
strict schema-constrained decoding. JSON-object mode is the common provider
contract; in the last resort we parse JSON out of prose. Both paths are
validated identically against the internal Pydantic schema.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import lru_cache
from time import monotonic

from jinja2 import Environment, PackageLoader, StrictUndefined
from pydantic import ValidationError

from overfit.config import LLMSettings, get_settings
from overfit.errors import OverfitError
from overfit.evaluation.citations import check_citation
from overfit.evaluation.contracts import TraceWriteError
from overfit.evaluation.trace import safe_error
from overfit.models import (
    Chunk,
    GeneratedExam,
    GeneratedItem,
    GeneratedMCQExam,
    GeneratedMCQItem,
)
from overfit.query.mcq_wire import (
    extract_json as _extract_json,
)
from overfit.query.mcq_wire import (
    normalize_mcq_wire as _normalize_mcq_wire,
)

__all__ = [
    "GenerationCallBudgetExceeded",
    "GenerationError",
    "GenerationResult",
    "Generator",
]


class GenerationError(OverfitError):
    """The model never produced output matching the schema."""


class GenerationCallBudgetExceeded(GenerationError):
    """A caller-owned provider-call budget was exhausted before dispatch."""


@dataclass
class GenerationResult:
    exam: GeneratedMCQExam | GeneratedExam
    attempts: int
    dropped: list[str]  # items rejected because their citation was invented
    raw: str = ""  # the model's last reply, kept for when the result is empty


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
        max_calls: int | None = None,
        on_token=None,
        recorder=None,
    ) -> GenerationResult:
        """Write practice questions from `chunks`."""
        system = _templates().get_template("system.j2").render()
        user = (
            _templates()
            .get_template("mock_prompt.j2")
            .render(course=course, chunks=chunks, count=count, topic=topic)
        )

        exam, attempts, raw = self._complete(
            system,
            user,
            GeneratedMCQExam,
            max_attempts or get_settings().max_retries + 1,
            max_calls=max_calls,
            max_items=count,
            on_token=on_token,
            **({"recorder": recorder} if recorder is not None else {}),
        )
        exam, dropped = _drop_invalid_mcq_citations(exam, chunks, recorder=recorder)
        return GenerationResult(
            exam=exam, attempts=attempts, dropped=dropped, raw=raw
        )

    # -- internals ---------------------------------------------------------

    def _stream(
        self, client, response_format, messages, on_token, *, recorder=None,
        attempt_index=1, format_index=1,
    ) -> str:
        """Collect a streamed reply, reporting progress as it arrives.

        Streaming is not an optimisation here, it is the difference between a
        usable tool and one that looks broken. A large local model spends its
        first minute loading weights and its next two generating; without any
        sign of life a user cannot tell a slow run from a hung one, and will
        reasonably kill it. Stream updates appearing are the only honest
        evidence that something is happening. The callback counts streamed
        delta chunks, not tokenizer tokens.
        """
        if recorder is not None:
            return self._traced_stream(
                client, response_format, messages, on_token, recorder,
                attempt_index, format_index,
            )
        pieces: list[str] = []
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
                    on_token(len(pieces), thinking)

            piece = delta.content or ""
            if piece:
                pieces.append(piece)
                if on_token:
                    on_token(len(pieces), thinking)
        return "".join(pieces)

    def _traced_stream(
        self, client, response_format, messages, on_token, recorder,
        attempt_index, format_index,
    ) -> str:
        """Observe a real request without exposing private reasoning or errors."""
        settings = get_settings()
        request = deepcopy({
            "model": self.model,
            "messages": messages,
            "temperature": settings.temperature,
            "response_format": response_format,
            "stream": True,
            "timeout": settings.request_timeout,
        })
        # Persist before calling the provider. Send this same frozen snapshot,
        # not the mutable conversation used to assemble the next retry.
        call_id = recorder.start_call(attempt_index, format_index, request)
        started = monotonic()
        pieces: list[str] = []
        thinking = 0
        usage = None
        try:
            stream = client.chat.completions.create(**request)
            for part in stream:
                observed_usage = _visible_usage(getattr(part, "usage", None))
                if observed_usage is not None:
                    usage = observed_usage
                if not part.choices:
                    continue
                delta = part.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None) or getattr(
                    delta, "reasoning", None
                )
                if reasoning:
                    thinking += 1
                    if on_token:
                        on_token(len(pieces), thinking)
                piece = delta.content or ""
                if piece:
                    pieces.append(piece)
                    if on_token:
                        on_token(len(pieces), thinking)
        except TraceWriteError:
            raise
        except (Exception, KeyboardInterrupt) as exc:
            recorder.emit("call_finished", {
                "call_id": call_id, "status": "error",
                "content": "".join(pieces), "response_complete": False,
                "elapsed_seconds": monotonic() - started,
                "error": safe_error(exc), "usage": usage,
            })
            raise
        content = "".join(pieces)
        recorder.emit("call_finished", {
            "call_id": call_id, "status": "completed", "content": content,
            "response_complete": True, "elapsed_seconds": monotonic() - started,
            "error": None, "usage": usage,
        })
        return content

    def _complete(
        self, system: str, user: str, schema, max_attempts: int, on_token=None,
        recorder=None, max_calls: int | None = None, max_items: int | None = None,
    ):
        """Call the model until the reply validates, or give up with detail."""
        if max_calls is not None and (type(max_calls) is not int or max_calls < 1):
            raise GenerationCallBudgetExceeded(
                "Generation provider-call budget exhausted before dispatch."
            )
        client = self._ensure_client()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        problems: list[str] = []
        calls_used = 0

        for attempt in range(1, max_attempts + 1):
            # JSON-object mode is the portable provider contract. If an older
            # endpoint rejects even that, fall back to parsing JSON from text.
            raw = None
            for format_index, response_format in enumerate(
                _response_formats(), start=1
            ):
                try:
                    if max_calls is not None and calls_used >= max_calls:
                        raise GenerationCallBudgetExceeded(
                            "Generation provider-call budget exhausted before dispatch."
                        )
                    trace_args = {} if recorder is None else {
                        "recorder": recorder, "attempt_index": attempt,
                        "format_index": format_index,
                    }
                    raw = self._stream(
                        client, response_format, messages, on_token, **trace_args
                    )
                    calls_used += 1
                    break
                except (TraceWriteError, GenerationCallBudgetExceeded):
                    raise
                except Exception as exc:  # noqa: BLE001 - endpoint capability probe
                    calls_used += 1
                    detail = str(exc) if recorder is None else "provider call failed"
                    problems.append(f"attempt {attempt}: {type(exc).__name__}: {detail}")
            if raw is None:
                continue

            if not raw.strip():
                # An empty body usually means the whole reply went to a
                # reasoning channel we did not read, not that the model had
                # nothing to say. Retrying without the schema often shakes it
                # loose; failing silently would look like "no questions".
                problems.append(f"attempt {attempt}: empty response body")
                if recorder is not None:
                    recorder.emit("validation_finished", {
                        "call_id": recorder.last_call_id, "status": "empty_body",
                        "item_count": None, "error": None,
                    })
                continue
            try:
                parsed = _extract_json(raw)
                if schema is GeneratedMCQExam:
                    parsed = _normalize_mcq_wire(parsed)
                else:
                    parsed = _coerce(parsed)
                exam = schema.model_validate(parsed)
                if max_items is not None and len(exam.items) > max_items:
                    raise ValueError(
                        f"response contains {len(exam.items)} items; maximum is {max_items}"
                    )
            except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as exc:
                if recorder is not None:
                    recorder.emit("validation_finished", {
                        "call_id": recorder.last_call_id, "status": "invalid",
                        "item_count": None, "error": safe_error(exc),
                    })
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
            else:
                if recorder is not None:
                    recorder.emit("validation_finished", {
                        "call_id": recorder.last_call_id, "status": "valid",
                        "item_count": len(exam.items), "error": None,
                    })
                return exam, attempt, raw

        if recorder is not None:
            raise GenerationError(
                f"{self.model} did not return valid output after {max_attempts} attempts."
            )
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
                "\n\nA timeout on a local model usually means memory, not the "
                "network: a 27B model needs roughly 17 GB, and anything else "
                "already loaded competes with it.\n"
                "  ollama ps                 # see what is resident\n"
                "  ollama stop <model>       # free the embedding model\n"
                "Or take a smaller step -- the provider is configuration, not "
                "code:\n"
                "  LLM_MODEL=qwen3:8b overfit mock ...\n"
                "  --material 6              # shorter prompt, less context\n"
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


def _response_formats():
    """Portable structured-output options, strongest first."""
    yield {"type": "json_object"}
    yield None  # unconstrained; _extract_json will dig the object out


_ITEM_KEYS = {"question", "answer", "source", "page"}
_QUESTION_LIST_KEYS = ("items", "questions", "exam", "results")


def _coerce(data: object) -> object:
    """Accept the shapes a model reasonably chooses instead of the exact one.

    The wire contract asks for `{"questions": [...]}`, while the internal
    model deliberately remains `GeneratedExam.items`. Older models and saved
    fixtures may still use `items`, a bare array, or one of the two historical
    explicit aliases `exam` and `results`.
    Every one of those carries exactly the information requested; rejecting
    them is pedantry that costs a retry, a minute, and tokens, and often
    fails again the same way.

    The rule is to coerce only allowlisted shapes. An arbitrary single list
    such as `{"warnings": []}` is not a question collection and must fail
    validation rather than silently becoming an empty exam.
    """
    if isinstance(data, list):
        return {"items": data}

    if isinstance(data, dict):
        for key in _QUESTION_LIST_KEYS:
            if key in data:
                value = data[key]
                return {"items": value} if key != "items" else data
        # A single item returned on its own, without any list around it.
        if _ITEM_KEYS.issubset(data.keys()):
            return {"items": [data]}

    return data


def _visible_usage(usage) -> dict | None:
    """Keep only known numeric counters, never arbitrary provider metadata."""
    if usage is None:
        return None
    result = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(field) if isinstance(usage, dict) else getattr(usage, field, None)
        if type(value) is int and value >= 0:
            result[field] = value
    return result or None


def _drop_invented_citations(
    exam: GeneratedExam, chunks: list[Chunk], *, recorder=None,
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
    materials = [asdict(chunk) for chunk in chunks] if recorder is not None else []

    def record(original_index, before, after, action, reason):
        if recorder is None:
            return
        post = after.model_dump() if after is not None else None
        recorder.emit("citation_processed", {
            "call_id": recorder.last_call_id,
            "item_id": f"{recorder.last_call_id}-item-{original_index}",
            "original_index": original_index,
            "final_index": len(kept) if post is not None else None,
            "pre_policy": before, "post_policy": post,
            "action": action, "reason": reason,
            "pre_check": check_citation(before, materials),
            "post_check": check_citation(post, materials) if post is not None else None,
        })

    for original_index, item in enumerate(exam.items, start=1):
        before = item.model_dump() if recorder is not None else None
        action, reason = "keep", "citation_supplied"
        if item.source not in allowed:
            dropped.append(f"{item.source} p{item.page} — no such file in the material")
            record(original_index, before, None, "drop", "unknown_source")
            continue
        if item.page not in allowed[item.source]:
            nearest = min(allowed[item.source], key=lambda p: abs(p - item.page))
            if abs(nearest - item.page) > 2:
                dropped.append(
                    f"{item.source} p{item.page} — page not among those supplied"
                )
                record(original_index, before, None, "drop", "page_not_supplied")
                continue
            item = item.model_copy(update={"page": nearest})
            action, reason = "repair", "nearby_page"
        kept.append(item)
        record(original_index, before, item, action, reason)

    return GeneratedExam(items=kept), dropped


def _drop_invalid_mcq_citations(
    exam: GeneratedMCQExam, chunks: list[Chunk], *, recorder=None,
) -> tuple[GeneratedMCQExam, list[str]]:
    """Atomically reject an MCQ if any option cites unsupplied material.

    Unlike the legacy short-answer policy, mcq-v1 never moves a citation to a
    nearby page: selecting a different page is a semantic change, not a wire
    normalization.  A broken option invalidates the whole four-option item.
    """
    allowed: dict[str, set[int]] = {}
    for chunk in chunks:
        pages = allowed.setdefault(chunk.source, set())
        pages.update(range(chunk.page, (chunk.page_end or chunk.page) + 1))

    kept: list[GeneratedMCQItem] = []
    dropped: list[str] = []
    for original_index, item in enumerate(exam.items, start=1):
        failure = None
        for label in "ABCD":
            option = item.options[label]
            if option.source not in allowed:
                failure = f"option {label}: {option.source} — no such file in the material"
                reason = "unknown_source"
                break
            cited_pages = set(range(option.page, (option.page_end or option.page) + 1))
            if not cited_pages.issubset(allowed[option.source]):
                shown = (
                    f"pp{option.page}-{option.page_end}"
                    if option.page_end is not None and option.page_end != option.page
                    else f"p{option.page}"
                )
                failure = f"option {label}: {option.source} {shown} — pages not supplied"
                reason = "page_not_supplied"
                break
        else:
            reason = "citation_supplied"

        if failure is None:
            kept.append(item)
        else:
            dropped.append(failure)

        if recorder is not None:
            post = item.model_dump() if failure is None else None
            recorder.emit("citation_processed", {
                "call_id": recorder.last_call_id,
                "item_id": f"{recorder.last_call_id}-item-{original_index}",
                "original_index": original_index,
                "final_index": len(kept) if post is not None else None,
                "pre_policy": item.model_dump(),
                "post_policy": post,
                "action": "keep" if post is not None else "drop",
                "reason": reason,
                "pre_check": None,
                "post_check": None,
            })

    return GeneratedMCQExam(items=kept), dropped


def render_exam(
    course: str,
    exam: GeneratedMCQExam | GeneratedExam,
    *,
    answers_file: str | None = None,
    generated_on: str | None = None,
) -> tuple[str, str]:
    """Render the question paper and the answer key."""
    if isinstance(exam, GeneratedMCQExam):
        return _render_mcq_exam(
            course, exam, answers_file=answers_file, generated_on=generated_on
        )

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
        "generated_at": generated_on or datetime.now(UTC).date().isoformat(),
        "answers_file": answers_file
        or get_settings().output_path(course, "answers.md").name,
    }
    env = _templates()
    return (
        env.get_template("mock_exam.md.j2").render(**context),
        env.get_template("mock_answers.md.j2").render(**context),
    )


def _render_mcq_exam(
    course: str,
    exam: GeneratedMCQExam,
    *,
    answers_file: str | None = None,
    generated_on: str | None = None,
) -> tuple[str, str]:
    context = _mcq_render_context(course, exam)
    context.update(
        generated_at=generated_on or datetime.now(UTC).date().isoformat(),
        answers_file=answers_file or get_settings().output_path(course, "answers.md").name,
    )
    env = _templates()
    return (
        env.get_template("mock_exam.md.j2").render(**context),
        env.get_template("mock_answers.md.j2").render(**context),
    )


def render_mcq_display(
    course: str,
    exam: GeneratedMCQExam,
    *,
    answers_file: str,
    generated_on: str,
) -> tuple[str, str]:
    """Render the student-facing paper and answer key, not the audit artifacts."""
    context = _mcq_render_context(course, exam)
    context.update(generated_at=generated_on, answers_file=answers_file)
    env = _templates()
    return (
        env.get_template("mcq_display_paper.md.j2").render(**context),
        env.get_template("mcq_display_answers.md.j2").render(**context),
    )


def _mcq_render_context(course: str, exam: GeneratedMCQExam) -> dict:
    numbered = []
    for index, item in enumerate(exam.items, start=1):
        options = []
        for label in "ABCD":
            option = item.options[label]
            options.append({
                "label": label,
                "text": option.text,
                "explanation": option.explanation,
                "citation": option.citation,
            })
        numbered.append({
            "number": index,
            "topic": item.topic,
            "question": item.question,
            "options": options,
            "correct_option": item.correct_option,
            "correct_text": item.correct.text,
        })

    return {
        "generation_contract": "mcq-v1",
        "course": course,
        "items": numbered,
        "sources": sorted({
            option.source
            for item in exam.items
            for option in item.options.values()
        }),
    }


def _summarise(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        return f"{location}: {first['msg']}"
    return str(exc)[:200]
