"""Command line entry point.

Every subcommand lives here and delegates to the pipeline layers.
Keep this file thin: argument parsing and presentation only, no business
logic. Anything worth unit-testing belongs in a layer module.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import SecretStr, ValidationError

from overfit.config import RerankSettings, get_settings
from overfit.errors import OverfitError, RerankConfigurationError, RerankError
from overfit.ingestion import chunker, loader, parser

app = typer.Typer(
    name="overfit",
    help="Turn course materials into structured, shareable study artifacts.",
    no_args_is_help=False,
)
eval_app = typer.Typer(help="Inspect traces offline or explicitly run an independent judge.")
app.add_typer(eval_app, name="eval")


@eval_app.command("replay")
def eval_replay(run_dir: Annotated[Path, typer.Argument(help="Recorded run directory.")]) -> None:
    """Validate a local trace and write a new offline replay report."""
    from overfit.evaluation.contracts import TraceReadError, TraceWriteError
    from overfit.evaluation.replay import replay_run
    from overfit.evaluation.trace import safe_error

    try:
        result = replay_run(run_dir)
    except (TraceReadError, TraceWriteError) as exc:
        typer.secho(safe_error(exc)["message"], fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    typer.echo(f"{result['run_status']} / {result['trace_integrity']}")
    typer.echo(str(result["replay_dir"]))
    if result["trace_integrity"] != "complete":
        raise typer.Exit(1)


def _judge_summary(result: dict, *, replay: bool = False) -> None:
    """Display the report itself, not just an opaque output directory."""
    metrics = result["metrics"]
    typer.echo(f"{'Judge replay' if replay else 'Judge review'} status: {result['status']}")
    if metrics["mode"] == "prepare":
        typer.echo("Prepare only: no judge calls; semantics not evaluated. Missing results do not mean incorrect questions.")
    typer.echo(
        f"Retained questions: {metrics['retained_count']}; "
        f"Selected reviews completed: {metrics['selected_evaluated_phase_count']}/{metrics['selected_phase_count']}; "
        f"Overall coverage: {metrics['evaluated_phase_count']}/{metrics['planned_phase_count']}"
    )
    typer.echo(f"Model positive on all three dimensions: {metrics['model_all_positive_count']} (not final approval); "
               f"Pending human review: {metrics['human_review_pending_item_count']} questions; "
               f"Technical errors: {metrics['technical_error_phase_count']} phases")
    typer.echo(f"{'Recorded model call attempts' if replay else 'Model call attempts'}: {metrics['call_starts']}; "
               f"Responses received: {metrics['response_count']}; Retries: {metrics['retry_count']}")
    if replay:
        typer.echo("Offline replay only; no new model calls.")
    typer.echo("Preliminary automated review; not yet human-calibrated.")
    typer.echo(f"Report: {Path(result['report_path']).expanduser().resolve()}")
    if result["exit_code"]:
        raise typer.Exit(result["exit_code"])


@eval_app.command("judge")
def eval_judge(
    run_dir: Annotated[Path, typer.Argument(help="Complete generation trace directory.")],
    execute: Annotated[bool, typer.Option("--execute", help="Explicitly call configured JUDGE service.")] = False,
    max_calls: Annotated[int, typer.Option("--max-calls", min=0, help="Hard call budget, including retries; required for execute.")] = 0,
    max_items: Annotated[int | None, typer.Option("--max-items", min=1, help="Review at most this many retained items; others remain in metrics.")] = None,
) -> None:
    """Prepare an offline report; use --execute to opt into independent model review."""
    from pydantic import ValidationError

    from overfit.evaluation.runner import JudgeRunError, run_judge

    try:
        result = run_judge(run_dir, execute=execute, max_calls=max_calls, max_items=max_items)
    except JudgeRunError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    except (OverfitError, ValidationError, OSError):
        typer.secho("Invalid judge configuration, input or recording; check the complete trace and independent JUDGE settings.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    _judge_summary(result)


@eval_app.command("judge-replay")
def eval_judge_replay(
    judgment_dir: Annotated[Path, typer.Argument(help="Existing judge recording directory.")],
) -> None:
    """Revalidate recorded judge responses and write a new report without model calls."""
    from overfit.evaluation.runner import JudgeRunError, replay_judgment

    try:
        result = replay_judgment(judgment_dir)
    except JudgeRunError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    except (OverfitError, OSError):
        typer.secho("Invalid or unreadable judge recording; offline replay was not completed.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    _judge_summary(result, replay=True)


@eval_app.command("judge-challenge")
def eval_judge_challenge(
    suite_dir: Annotated[Path, typer.Argument(help="Independent challenge inputs/annotations directory.")],
    execute: Annotated[bool, typer.Option("--execute", help="Explicitly call the JUDGE service.")] = False,
    max_calls: Annotated[int, typer.Option("--max-calls", min=0, help="Global call budget including retries; required for execute.")] = 0,
    max_items: Annotated[int | None, typer.Option("--max-items", min=1, help="Select the first N cases; retain all others as unreviewed.")] = None,
    output_dir: Annotated[Path | None, typer.Option("--output-dir", help="Output parent; a fresh UUID child is always created.")] = None,
) -> None:
    """Prepare a standalone challenge, or explicitly execute it without creating a fake trace."""
    from overfit.evaluation.challenge import ChallengeError, run_challenge

    try:
        result = run_challenge(suite_dir, execute=execute, max_calls=max_calls,
                               max_items=max_items, output_dir=output_dir)
    except ChallengeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    except (OverfitError, OSError, ValueError):
        typer.secho("Invalid challenge input, configuration or recording; this experiment was not completed.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    metrics = result["metrics"]
    typer.echo(f"Judge challenge status: {result['status']} (not a formal generation evaluation)")
    if metrics["mode"] == "prepare":
        typer.echo("Offline preparation only: no model calls; all semantics remain unevaluated.")
    typer.echo(f"Cases: {metrics['case_count']}; Selected reviews completed: "
               f"{metrics['selected_evaluated_phase_count']}/{metrics['selected_phase_count']}; "
               f"Overall coverage: {metrics['evaluated_phase_count']}/{metrics['planned_phase_count']}")
    typer.echo(f"Model positive on all three dimensions: {metrics['model_all_positive_case_count']} (not final approval); "
               f"Technical errors: {metrics['technical_error_phase_count']} phases; "
               f"Pending human review: {metrics['human_review_pending_case_count']} cases")
    typer.echo(f"Model call attempts: {metrics['call_starts']}; Responses received: {metrics['response_count']}; Retries: {metrics['retry_count']}")
    typer.echo("Human-designed targets are not model results; no automatic target-matching score is calculated.")
    typer.echo("Preliminary automated review; not yet human-calibrated.")
    typer.echo(f"Report: {Path(result['report_path']).resolve()}")
    if result["exit_code"]:
        raise typer.Exit(result["exit_code"])


@eval_app.command("generator-challenge")
def eval_generator_challenge(
    suite_dir: Annotated[
        Path, typer.Argument(help="Controlled Generator fixture suite directory.")
    ],
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Explicitly call the configured Generator."),
    ] = False,
    max_calls: Annotated[
        int,
        typer.Option(
            "--max-calls",
            min=0,
            help="Global provider-call budget including format fallbacks.",
        ),
    ] = 0,
    max_items: Annotated[
        int | None,
        typer.Option("--max-items", min=1, help="Execute only the first N cases."),
    ] = None,
    max_attempts: Annotated[
        int,
        typer.Option("--max-attempts", min=1, help="Validation attempts per case."),
    ] = 2,
) -> None:
    """Prepare or explicitly execute isolated Generator material fixtures."""
    from overfit.evaluation.generator_challenge import (
        GeneratorChallengeError,
        run_generator_challenge,
    )

    try:
        settings = get_settings() if execute else None
        result = run_generator_challenge(
            suite_dir,
            execute=execute,
            max_calls=max_calls,
            max_items=max_items,
            max_attempts=max_attempts,
            output_dir=Path("outputs"),
            settings=settings,
        )
    except (GeneratorChallengeError, ValidationError, OSError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None

    typer.echo(f"Generator challenge status: {result['status']}")
    typer.echo(
        f"Cases: {len(result['records'])}/{result['selected_count']}; "
        f"provider calls: {result['call_count']}/{result['max_calls']}"
    )
    if not execute:
        typer.echo("Prepare only: no settings, keys, or model calls were used.")
    typer.echo(f"Report: {Path(result['report_path']).resolve()}")
    if result["exit_code"]:
        raise typer.Exit(result["exit_code"])


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo("hello overfit")


CourseArg = Annotated[str, typer.Option("--course", "-c", help="Course folder name.")]
PathOpt = Annotated[
    Path | None,
    typer.Option("--path", "-p", help="Read from this directory instead of COURSES_DIR."),
]


def _make_rerank_service(
    settings,
    *,
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float | None = None,
):
    """Apply one-invocation CLI overrides and validate before opening an index."""
    from overfit.query.rerankers.openrouter import OpenRouterReranker
    from overfit.query.rerankers.service import RerankService

    values = settings.rerank.model_dump()
    updates = {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "timeout": timeout,
    }
    values.update({key: value for key, value in updates.items() if value is not None})
    if api_key is not None:
        values["api_key"] = SecretStr(api_key)
    try:
        config = RerankSettings(_env_file=None, **values).for_query()
    except ValidationError:
        raise RerankConfigurationError(
            "Invalid reranker configuration: provider must be openrouter, "
            "model must be non-empty, base URL must use HTTPS, and timeout "
            "must be positive."
        ) from None
    adapter = OpenRouterReranker(
        api_key=config.api_key.get_secret_value(),
        model=config.model,
        base_url=config.base_url,
        timeout=config.timeout,
    )
    return RerankService(
        adapter,
        output_dir=settings.outputs_dir,
        timeout=config.timeout,
    )


def _report_rerank(result) -> None:
    if result.status.value == "skipped":
        typer.secho(
            "[reranker] skipped reason=no_query", fg=typer.colors.CYAN, err=True
        )
        return
    typer.secho(
        f"[reranker] applied provider={result.provider} model={result.model} "
        f"candidates={len(result.ranked)} "
        f"selected={len(result.selected)} attempts={result.attempts} "
        f"latency_ms={result.latency_ms:.0f}",
        fg=typer.colors.CYAN,
        err=True,
    )


def _report_rerank_skipped() -> None:
    typer.secho("[reranker] skipped reason=no_query", fg=typer.colors.CYAN, err=True)


def _report_rerank_failure(exc: OverfitError) -> None:
    if isinstance(exc, RerankError):
        typer.secho(
            f"[reranker] failed attempts={exc.attempts}; generation aborted",
            fg=typer.colors.RED,
            err=True,
        )


@app.command()
def inspect(
    course: CourseArg,
    path: PathOpt = None,
    samples: Annotated[int, typer.Option(help="Pages to print in full.")] = 2,
    chars: Annotated[int, typer.Option(help="Characters per sample.")] = 600,
    show_removed: Annotated[
        bool,
        typer.Option("--show-removed", help="Print what cleaning deleted, per file."),
    ] = False,
) -> None:
    """Show what the parser actually extracts, before anything is indexed.

    This exists because the single most common cause of a bad RAG system is
    text that was already garbage when it left layer 2 -- and nobody looks.
    Run this on real material before tuning chunk sizes or prompts.
    """
    settings = get_settings()
    directory = path.expanduser().resolve() if path else settings.course_dir(course)

    try:
        files = loader.find_documents(directory, settings.extension_list)
    except OverfitError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    typer.secho(f"\n{directory}", fg=typer.colors.CYAN)
    typer.echo(f"{len(files)} document(s) found")
    _report_ignored(directory, settings)
    typer.echo()

    failures: list[str] = []
    total_pages = total_chars = 0

    for file in files:
        relative = file.relative_to(directory)
        try:
            document = parser.parse(
                file,
                source=file.relative_to(directory).as_posix(),
                backend=settings.pdf_backend,
            )
        except OverfitError as exc:
            failures.append(str(relative))
            typer.secho(f"  FAIL  {relative}", fg=typer.colors.RED)
            typer.echo(f"        {exc}")
            continue

        page_chars = [len(page.text) for page in document.pages]
        chars_total = sum(page_chars)
        total_pages += len(document.pages)
        total_chars += chars_total

        flag = ""
        if parser.is_probably_scanned(document):
            flag = "  <- mostly empty, check for a scan"

        typer.secho(f"  OK    {relative}", fg=typer.colors.GREEN)
        typer.echo(
            f"        {len(document.pages)} pages, {chars_total:,} chars, "
            f"median {_median(page_chars):,}/page{flag}"
        )
        blank = sum(1 for n in page_chars if n < 20)
        if blank:
            typer.echo(f"        {blank} near-empty page(s)")

        if show_removed:
            _report_removed(file, str(relative), settings.pdf_backend)

    if total_pages:
        # No chunk estimate here. Dividing total characters by the chunk size
        # is wrong by a factor of three on slide decks, because chunking
        # respects page boundaries and most slides fall well under the
        # target. Run `overfit chunks` for the real count.
        typer.echo(
            f"\ntotal: {total_pages} pages, {total_chars:,} chars, "
            f"~{total_chars // 4:,} tokens"
        )
    if failures:
        typer.secho(f"{len(failures)} file(s) could not be read", fg=typer.colors.RED)

    # Print real text last, so it is the thing left on screen.
    for file in files[:samples]:
        try:
            document = parser.parse(
                file,
                source=file.relative_to(directory).as_posix(),
                backend=settings.pdf_backend,
            )
        except OverfitError:
            continue
        page = max(document.pages, key=lambda p: len(p.text))
        typer.secho(
            f"\n--- {file.name} p{page.number} (longest page) ---",
            fg=typer.colors.YELLOW,
        )
        typer.echo(page.text[:chars])


@app.command()
def chunks(
    course: CourseArg,
    path: PathOpt = None,
    show: Annotated[int, typer.Option(help="Chunks to print in full.")] = 5,
    spanning: Annotated[bool, typer.Option(help="Only show chunks that cross a page.")] = False,
) -> None:
    """Preview how documents are cut up, before anything is embedded.

    Chunking is where most RAG systems quietly go wrong, and the only
    reliable check is a human reading a handful of real chunks. Look for
    thoughts severed mid-argument and for chunks that mix two unrelated
    topics -- the first wastes context, the second poisons retrieval.
    """
    settings = get_settings()
    directory = path.expanduser().resolve() if path else settings.course_dir(course)

    try:
        files = loader.find_documents(directory, settings.extension_list)
    except OverfitError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    typer.secho(f"\n{directory}", fg=typer.colors.CYAN)
    typer.echo(
        f"chunk_size={settings.chunk_size} tokens "
        f"(~{settings.chunk_size * chunker.CHARS_PER_TOKEN} chars), "
        f"overlap={settings.chunk_overlap}\n"
    )

    all_chunks: list = []
    for file in files:
        try:
            document = parser.parse(
                file,
                source=file.relative_to(directory).as_posix(),
                backend=settings.pdf_backend,
            )
        except OverfitError as exc:
            typer.secho(f"  skip  {file.name}: {exc}", fg=typer.colors.RED)
            continue

        produced = chunker.chunk_document(
            document, settings.chunk_size, settings.chunk_overlap
        )
        crossing = sum(1 for c in produced if c.page_end)
        typer.secho(f"  {file.name}", fg=typer.colors.GREEN)
        summary = f"      {len(document.pages)} pages -> {len(produced)} chunks"
        if produced:
            sizes = sorted(len(c.text) for c in produced)
            summary += (
                f"   |  chars min {sizes[0]} / median {sizes[len(sizes) // 2]}"
                f" / max {sizes[-1]}"
            )
        typer.echo(f"{summary}   |  {crossing} span a page break")
        all_chunks.extend(produced)

    if not all_chunks:
        return

    sizes = sorted(len(c.text) for c in all_chunks)
    typer.echo(
        f"\ntotal {len(all_chunks)} chunks   "
        f"median {sizes[len(sizes) // 2]} chars   "
        f"{sum(1 for c in all_chunks if c.page_end)} spanning"
    )

    sample = [c for c in all_chunks if c.page_end] if spanning else all_chunks
    step = max(len(sample) // max(show, 1), 1)
    for chunk in sample[::step][:show]:
        typer.secho(
            f"\n--- {chunk.citation}  [{chunk.id}]  {len(chunk.text)} chars ---",
            fg=typer.colors.YELLOW,
        )
        typer.echo(chunk.text)


def _open_store(course: str, *, create: bool = False):
    """Open the index for a course, with a profile built from live settings.

    The dimension is probed from the model rather than configured, so the
    table is always created at the width the model actually returns.
    """
    from overfit.embedding import get_embedder
    from overfit.storage.store import IndexProfile, VectorStore

    settings = get_settings()
    embedder = get_embedder()
    path = settings.db_path(course)
    embed_dim = (
        embedder.dimension
        if create
        else VectorStore.read_profile(path, course=course).embed_dim
    )
    profile = IndexProfile(
        embed_model=embedder.model,
        embed_dim=embed_dim,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        parser=settings.pdf_backend,
    )
    store = VectorStore.open(
        path, profile, course=course, create=create
    )
    return store, embedder, settings


@eval_app.command("rerank")
def eval_rerank(
    course: Annotated[str, typer.Option("--course", "-c", help="Indexed course name.")],
    dataset: Annotated[
        Path,
        typer.Option("--dataset", help="Frozen retrieval evaluation JSON."),
    ] = Path("eval/ifn580_eval.json"),
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Explicitly call the configured reranker."),
    ] = False,
    resume: Annotated[
        Path | None,
        typer.Option("--resume", help="Explicitly resume an incomplete run directory."),
    ] = None,
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="Ignore exact-request evaluation cache entries."),
    ] = False,
    rerank_provider: Annotated[str | None, typer.Option("--rerank-provider")] = None,
    rerank_api_key: Annotated[str | None, typer.Option("--rerank-api-key")] = None,
    rerank_model: Annotated[str | None, typer.Option("--rerank-model")] = None,
    rerank_base_url: Annotated[str | None, typer.Option("--rerank-base-url")] = None,
    rerank_timeout: Annotated[
        float | None, typer.Option("--rerank-timeout", min=0.001)
    ] = None,
) -> None:
    """Run the formal 47-item, two-track dense Top-40 → rerank Top-10 eval."""
    if not execute:
        typer.secho(
            "No calls made. Formal rerank evaluation requires explicit --execute.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)

    from overfit.evaluation.rerank_runner import (
        RerankEvalRunError,
        run_rerank_evaluation,
    )

    try:
        settings = get_settings()
        service = _make_rerank_service(
            settings,
            provider=rerank_provider,
            api_key=rerank_api_key,
            model=rerank_model,
            base_url=rerank_base_url,
            timeout=rerank_timeout,
        )
        store, embedder, settings = _open_store(course)
        with store:
            result = run_rerank_evaluation(
                course=course,
                dataset_path=dataset,
                store=store,
                embedder=embedder,
                reranker=service,
                output_dir=settings.outputs_dir,
                execute=True,
                resume=resume,
                no_cache=no_cache,
                index_path=settings.db_path(course),
            )
    except (OverfitError, RerankEvalRunError, OSError, ValidationError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None

    typer.echo(f"Rerank evaluation status: {result['status']}")
    typer.echo(f"Report: {result['run_dir'] / 'report.md'}")
    if result["status"] != "complete":
        typer.secho(
            "Run is incomplete; inspect the report and use --resume explicitly.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)


@app.command()
def ingest(
    course: CourseArg,
    path: PathOpt = None,
    force: Annotated[bool, typer.Option(help="Re-embed even unchanged files.")] = False,
    rebuild: Annotated[bool, typer.Option(help="Delete the index and start over.")] = False,
) -> None:
    """Build the searchable index for a course.

    Safe to re-run: files whose contents have not changed are skipped, so a
    second run costs almost nothing. Use --force after changing a cleaning
    rule, and --rebuild after changing the embedding model or chunk size.
    """
    from overfit.ingestion import pipeline

    settings = get_settings()
    directory = path.expanduser().resolve() if path else settings.course_dir(course)
    settings.ensure_dirs()

    if rebuild:
        target = settings.db_path(course)
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(target) + suffix)
            candidate.unlink(missing_ok=True)
        typer.secho(f"removed {target.name}", fg=typer.colors.YELLOW)

    try:
        store, embedder, settings = _open_store(course, create=True)
    except OverfitError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    typer.secho(f"\n{directory}", fg=typer.colors.CYAN)
    typer.echo(f"{embedder.model} -> {embedder.dimension}d   into {settings.db_path(course).name}")
    _report_ignored(directory, settings)
    typer.echo()

    total = 0

    def announce(position: int, count: int, file: Path) -> None:
        nonlocal total
        total = count
        # Overwrite this line once the file is done, so the terminal shows
        # what is happening now rather than a growing wall of history.
        typer.echo(f"  [{position}/{count}] reading {file.name[:48]}...\r", nl=False)

    def report_one(outcome) -> None:
        colour = {
            "indexed": typer.colors.GREEN,
            "cached": typer.colors.BLUE,
            "failed": typer.colors.RED,
        }[outcome.status]
        suffix = f"  ({outcome.chunks} chunks)" if outcome.chunks else ""
        typer.secho(f"\r  {outcome.status:9}{outcome.name}{suffix}", fg=colour)
        if outcome.detail:
            typer.echo(f"            {outcome.detail}")

    try:
        with store:
            report = pipeline.ingest(
                directory,
                store,
                embedder,
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
                extensions=settings.extension_list,
                pdf_backend=settings.pdf_backend,
                force=force,
                on_file=announce,
                on_result=report_one,
            )
            stats = store.stats()
    except OverfitError as exc:
        typer.secho(f"\n{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    typer.echo(
        f"\n{report.count('indexed')} indexed, "
        f"{report.count('cached')} unchanged, "
        f"{report.count('failed')} failed"
        f"   |  {report.embeddings_computed} embeddings in {report.seconds:.1f}s"
    )
    typer.echo(f"index now holds {stats['chunks']} chunks from {stats['documents']} files")
    if not stats["consistent"]:
        typer.secho("WARNING: chunk and vector counts disagree", fg=typer.colors.RED)


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="What to look for.")],
    course: CourseArg,
    top_k: Annotated[
        int, typer.Option("--top-k", "-k", help="Results to show.", min=1)
    ] = 10,
    chars: Annotated[int, typer.Option(help="Characters of each chunk to print.")] = 300,
    rerank_provider: Annotated[str | None, typer.Option("--rerank-provider")] = None,
    rerank_api_key: Annotated[str | None, typer.Option("--rerank-api-key")] = None,
    rerank_model: Annotated[str | None, typer.Option("--rerank-model")] = None,
    rerank_base_url: Annotated[str | None, typer.Option("--rerank-base-url")] = None,
    rerank_timeout: Annotated[float | None, typer.Option("--rerank-timeout", min=0.001)] = None,
) -> None:
    """Search the index directly, without generating anything.

    The fastest way to tell whether a disappointing answer is the retriever's
    fault or the model's. If the right material is not in this list, no
    prompt will rescue the output.
    """
    from overfit.query import retriever

    try:
        settings = get_settings()
        reranker = _make_rerank_service(
            settings,
            provider=rerank_provider,
            api_key=rerank_api_key,
            model=rerank_model,
            base_url=rerank_base_url,
            timeout=rerank_timeout,
        )
        store, embedder, _ = _open_store(course)
        with store:
            hits = retriever.retrieve(
                query,
                store,
                embedder,
                top_k=top_k,
                fetch_k=40,
                reranker=reranker,
                on_rerank=_report_rerank,
            )
    except OverfitError as exc:
        _report_rerank_failure(exc)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    typer.secho(f'\n"{query}"', fg=typer.colors.CYAN)
    typer.echo(f"{len(hits)} of {top_k} requested\n")

    for rank, hit in enumerate(hits, start=1):
        typer.secho(
            f"{rank}. rerank={hit.rerank_score:+.3f} (r{hit.rerank_rank})  "
            f"vector={hit.vector_score:+.3f} (r{hit.vector_rank})  {hit.chunk.citation}",
            fg=typer.colors.GREEN,
        )
        text = " ".join(hit.chunk.text.split())
        typer.echo(f"   {text[:chars]}{'...' if len(text) > chars else ''}\n")


@app.command()
def mock(
    course: CourseArg,
    questions: Annotated[
        int | None,
        typer.Option("--questions", "-q", help="How many (default: 30 without --topic)."),
    ] = None,
    topic: Annotated[str | None, typer.Option(help="Restrict to one subject.")] = None,
    material: Annotated[int, typer.Option(help="Passages to draw from.")] = 0,
    trace: Annotated[bool, typer.Option("--trace", help="Record an auditable generation run.")] = False,
    judge: Annotated[
        bool,
        typer.Option("--judge", help="Explicitly run the independent MCQ Judge after generation."),
    ] = False,
    rerank_provider: Annotated[str | None, typer.Option("--rerank-provider")] = None,
    rerank_api_key: Annotated[str | None, typer.Option("--rerank-api-key")] = None,
    rerank_model: Annotated[str | None, typer.Option("--rerank-model")] = None,
    rerank_base_url: Annotated[str | None, typer.Option("--rerank-base-url")] = None,
    rerank_timeout: Annotated[float | None, typer.Option("--rerank-timeout", min=0.001)] = None,
) -> None:
    """Write a practice exam from the course material.

    Selects more passages than questions on purpose: some of what a course
    folder contains -- notices, acknowledgements, table fragments -- cannot
    support a question, and the model needs room to skip them rather than
    being forced to pad.
    """
    if topic and questions is None:
        raise typer.BadParameter(
            "--questions is required when --topic is supplied.",
            param_hint="--questions",
        )
    questions = 30 if questions is None else questions
    settings = get_settings()
    judge_settings = None
    if judge:
        from overfit.evaluation.judge import JudgeSettings

        try:
            judge_settings = JudgeSettings()
        except (ValidationError, ValueError):
            typer.secho(
                "Invalid judge configuration; set JUDGE_MODEL, JUDGE_BASE_URL, "
                "JUDGE_API_KEY and valid limits.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1) from None
    try:
        reranker = (
            _make_rerank_service(
                settings,
                provider=rerank_provider,
                api_key=rerank_api_key,
                model=rerank_model,
                base_url=rerank_base_url,
                timeout=rerank_timeout,
            )
            if topic
            else None
        )
    except OverfitError as exc:
        _report_rerank_failure(exc)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    if not topic:
        _report_rerank_skipped()
    from overfit.evaluation.live import run_mock_trace

    result = run_mock_trace(
        settings,
        _open_store,
        course=course,
        questions=questions,
        topic=topic,
        material=material,
        reranker=reranker,
        on_rerank=_report_rerank if topic else None,
    )
    if result["error"] is not None:
        if result.get("rerank_failed"):
            typer.secho(
                f"[reranker] failed attempts={result['rerank_attempts']}; generation aborted",
                fg=typer.colors.RED,
                err=True,
            )
        typer.secho(result["error"]["message"], fg=typer.colors.RED, err=True)
    typer.echo(f"Trace status: {result['status']}")
    if result["run_dir"] is not None and trace:
        typer.echo(result["run_dir"])
    if result["exit_code"]:
        raise typer.Exit(result["exit_code"])
    if result["run_dir"] is None:
        raise typer.Exit(0)

    run_dir = Path(result["run_dir"])
    summary = json.loads((run_dir / "result.json").read_text())
    typer.secho(f"{summary['retained_count']} MCQ questions", fg=typer.colors.GREEN)
    if not trace and not judge and result["status"] == "completed":
        settings.ensure_dirs()
        paper_path = settings.output_path(course, "mock_exam.md")
        answers_path = settings.output_path(course, "answers.md")
        paper_path.write_text((run_dir / paper_path.name).read_text(), encoding="utf-8")
        answers_path.write_text((run_dir / answers_path.name).read_text(), encoding="utf-8")
        typer.echo(f"\n  {paper_path}\n  {answers_path}")

    if judge:
        from overfit.evaluation.runner import JudgeRunError, run_judge

        try:
            review = run_judge(run_dir, execute=True, settings=judge_settings)
        except JudgeRunError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
        _judge_summary(review)
        if not trace and review["publication"] is not None:
            settings.ensure_dirs()
            paper_path = settings.output_path(course, "mock_exam.md")
            answers_path = settings.output_path(course, "answers.md")
            paper_path.write_text(
                Path(review["publication"]["paper"]).read_text(), encoding="utf-8"
            )
            answers_path.write_text(
                Path(review["publication"]["answers"]).read_text(), encoding="utf-8"
            )
            typer.echo(f"\n  {paper_path}\n  {answers_path}")
        typer.echo(
            f"Judged publication retained {review['metrics']['accepted_count']} questions."
        )
    else:
        typer.echo("MCQ structure validated")
        typer.echo("Semantic uniqueness and distractor correctness: not judged")


@app.command()
def topics(
    course: CourseArg,
    count: Annotated[int, typer.Option("--count", "-n", help="Topics to identify.")] = 12,
    questions: Annotated[int, typer.Option(help="Show how N questions would be split.")] = 10,
) -> None:
    """Rank what the unit spends its time on, heaviest first.

    Weight counts distinct *files* covering a topic, not chunks. A subject
    taught in a lecture, drilled in a tutorial and applied in a practical was
    returned to three times; one long handout repeating itself was not. The
    difference matters, and chunk counts cannot see it.

    Ranking uses only the stored vectors and persisted index profile. Neither
    the embedding nor generation model is called, and every figure is
    checkable against the files.
    """
    from overfit.query import retriever, selection

    try:
        store, _, _ = _open_store(course)
        with store:
            found = retriever.rank_topics(store, count)
            total = store.stats()["chunks"]
    except OverfitError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    shares = {
        id(topic): share for topic, share in selection.allocate(found, questions)
    }

    typer.secho(f"\n{len(found)} topics over {total} chunks\n", fg=typer.colors.CYAN)
    typer.echo(f"  {'files':>5} {'chunks':>6} {'qs':>4}   topic")
    for topic in found:
        share = shares.get(id(topic), 0)
        colour = typer.colors.GREEN if topic.weight >= 3 else typer.colors.WHITE
        typer.secho(
            f"  {topic.weight:>5} {topic.size:>6} {share:>4}   {topic.label}",
            fg=colour,
        )

    typer.echo(
        f"\n'files' is how many different documents cover the topic -- the "
        f"signal that a unit keeps returning to something.\n"
        f"'qs' is how {questions} questions would be apportioned."
    )


@app.command()
def coverage(
    course: CourseArg,
    count: Annotated[
        int | None,
        typer.Option(
            "--count",
            "-n",
            help="Chunks to select (default: 10 with topic, 12 without).",
            min=1,
        ),
    ] = None,
    topic: Annotated[str | None, typer.Option(help="Restrict to a topic first.")] = None,
    chars: Annotated[int, typer.Option(help="Characters of each chunk to print.")] = 180,
    rerank_provider: Annotated[str | None, typer.Option("--rerank-provider")] = None,
    rerank_api_key: Annotated[str | None, typer.Option("--rerank-api-key")] = None,
    rerank_model: Annotated[str | None, typer.Option("--rerank-model")] = None,
    rerank_base_url: Annotated[str | None, typer.Option("--rerank-base-url")] = None,
    rerank_timeout: Annotated[float | None, typer.Option("--rerank-timeout", min=0.001)] = None,
) -> None:
    """Preview the material a generation command would be given.

    This is the exam's syllabus before a single question exists. If the
    selection here misses half the unit, no prompt will produce a balanced
    paper -- and reading twelve chunks is far cheaper than reading ten
    questions and inferring what went wrong.
    """
    from overfit.query import retriever

    try:
        settings = get_settings()
        reranker = (
            _make_rerank_service(
                settings,
                provider=rerank_provider,
                api_key=rerank_api_key,
                model=rerank_model,
                base_url=rerank_base_url,
                timeout=rerank_timeout,
            )
            if topic
            else None
        )
        if not topic:
            _report_rerank_skipped()
        budget = count if count is not None else (10 if topic else 12)
        store, embedder, _ = _open_store(course)
        with store:
            total = store.stats()["chunks"]
            picked = retriever.gather_material(
                store,
                embedder,
                count=budget,
                topic=topic,
                reranker=reranker,
                on_rerank=_report_rerank if topic else None,
            )
    except OverfitError as exc:
        _report_rerank_failure(exc)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    label = f'topic "{topic}"' if topic else "whole course"
    typer.secho(f"\n{len(picked)} of {total} chunks   ({label})\n", fg=typer.colors.CYAN)

    by_source: dict[str, int] = {}
    for chunk in picked:
        by_source[chunk.source] = by_source.get(chunk.source, 0) + 1
        text = " ".join(chunk.text.split())
        typer.secho(f"  {chunk.citation}", fg=typer.colors.GREEN)
        typer.echo(f"    {text[:chars]}{'...' if len(text) > chars else ''}")

    typer.echo("\nspread across files:")
    for name, number in sorted(by_source.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {number:>3}  {name}")


@app.command()
def status(course: CourseArg) -> None:
    """Show what an index contains and how it was built."""
    try:
        store, _, settings = _open_store(course)
    except OverfitError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    with store:
        stats = store.stats()
        sources = store.sources()

    typer.secho(f"\n{settings.db_path(course)}", fg=typer.colors.CYAN)
    for key in ("parser", "embed_model", "embed_dim", "chunk_size", "chunk_overlap"):
        # An index built before a key existed simply does not carry it, and
        # saying so is better than crashing on a dictionary lookup.
        typer.echo(f"  {key:<14} {stats.get(key, '(not recorded)')}")
    typer.echo(f"  {'chunks':<14} {stats['chunks']}")
    typer.echo(f"  {'documents':<14} {stats['documents']}\n")
    for name, count in sources:
        typer.echo(f"  {count:>4}  {name}")


@app.command("embed-check")
def embed_check() -> None:
    """Verify the embedding model works, and that it works *semantically*.

    Two things are checked. First that the endpoint answers at all, and what
    vector width it returns -- the number the store's schema will be built
    from. Second, and more interesting, that similarity behaves the way the
    whole system assumes: related sentences must score higher than unrelated
    ones, and a Chinese question must match English course material, since
    that is how this tool is actually used.
    """
    from overfit.embedding import get_embedder

    embedder = get_embedder()
    typer.secho(f"\n{embedder.model} @ {embedder.base_url}", fg=typer.colors.CYAN)

    try:
        dimension = embedder.dimension
    except OverfitError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"dimension: {dimension}\n")

    anchor = "Overfitting happens when a model learns noise in the training set."
    probes = [
        ("near paraphrase", "A model that memorises training data generalises poorly."),
        ("same topic", "Regularisation reduces variance in a machine learning model."),
        ("related but different", "Underfitting means the model is too simple."),
        ("Chinese query", "什么是过拟合"),
        ("unrelated", "The assignment is due in week 7 and is worth 20 marks."),
        ("nonsense", "Bread milk eggs shopping list Tuesday."),
    ]

    vectors = embedder.embed([anchor] + [text for _, text in probes])
    base, rest = vectors[0], vectors[1:]

    typer.echo(f"anchor: {anchor}\n")
    for (label, text), vector in zip(probes, rest):
        score = sum(a * b for a, b in zip(base, vector))
        bar = "#" * max(int(score * 40), 0)
        colour = typer.colors.GREEN if score > 0.5 else typer.colors.WHITE
        typer.secho(f"  {score:+.3f}  {bar:<40} {label}", fg=colour)
        typer.echo(f"          {text}")

    typer.echo(
        "\nExpect the paraphrase highest and the shopping list lowest. If the "
        "Chinese probe scores near zero, cross-language retrieval will not "
        "work and the embedding model needs reconsidering."
    )


def _report_ignored(directory: Path, settings) -> None:
    """Say what was passed over, so a silent filter cannot become a mystery."""
    ignored = loader.count_ignored(directory, settings.extension_list)
    if not ignored:
        return
    summary = ", ".join(f"{count} {ext}" for ext, count in sorted(ignored.items()))
    typer.secho(
        f"  skipping {summary}  (EXTENSIONS={settings.extensions})",
        fg=typer.colors.YELLOW,
    )


def _report_removed(
    file: Path, label: str, backend: str = "pypdf", preview: int = 6
) -> None:
    """Show what layer 2 deleted from one document.

    Deliberately prints the text rather than a rate. Cleaning cannot be
    scored -- there is no correct amount -- so the useful output is evidence
    a human can disagree with. A percentage says too much went; only the
    lines themselves say the wrong thing went.
    """
    try:
        report = parser.cleaning_report(file, source=label, backend=backend)
    except OverfitError:
        return

    if not report.removals:
        typer.echo("        cleaning removed nothing")
        return

    typer.echo(
        f"        cleaning removed {report.removed_ratio:.1%} "
        f"({report.chars_before:,} -> {report.chars_after:,} chars)"
    )

    if not report.numbers_preserved:
        # Nothing else in this output matters if this line ever appears.
        typer.secho(
            "        PAGE NUMBERS CHANGED DURING CLEANING -- citations are unreliable",
            fg=typer.colors.RED,
        )

    if report.furniture and report.furniture_labelled:
        numbers, named = report.furniture_lines()
        total = len(report.by_stage("furniture"))
        typer.echo(
            f"        running furniture (labelled by the backend): "
            f"{total} line(s) removed"
        )
        if numbers:
            typer.echo(f"          {numbers:>3}   bare page numbers")
        for line, count in named[:preview]:
            typer.echo(f"          {count:>3}x  {line[:64]!r}")
        if len(named) > preview:
            typer.echo(f"          ... {len(named) - preview} more")

    elif report.furniture:
        typer.echo("        running furniture (detected at 60% of pages):")
        for line, count in sorted(report.furniture.items(), key=lambda kv: -kv[1]):
            share = report.margin(line)
            hits = len(
                [r for r in report.by_stage("furniture") if r.text == line.strip()]
            )
            # A line that only just cleared 60% is a judgement the code made
            # narrowly, and narrow calls are where the wrong deletions are.
            marginal = share < 0.75
            typer.secho(
                f"          {count:>3}/{report.pages} pages ({share:.0%})"
                f"  {hits:>3} removed   {line[:64]!r}"
                f"{'   <- borderline, check this' if marginal else ''}",
                fg=typer.colors.YELLOW if marginal else None,
            )

    numbers = report.by_stage("page-number")
    if numbers:
        typer.echo(f"        page numbers stripped: {len(numbers)} line(s)")

    debris = report.by_stage("figure-debris")
    if debris:
        pages = sorted({item.page for item in debris})
        typer.echo(
            f"        figure debris: {len(debris)} line(s) across "
            f"{len(pages)} page(s)"
        )
        for item in debris[:preview]:
            typer.echo(f"          p{item.page}  {item.text[:64]!r}")
        if len(debris) > preview:
            typer.echo(f"          ... {len(debris) - preview} more")


def _median(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
