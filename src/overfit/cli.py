"""Command line entry point.

Every subcommand lives here and delegates to the pipeline layers.
Keep this file thin: argument parsing and presentation only, no business
logic. Anything worth unit-testing belongs in a layer module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from overfit.config import get_settings
from overfit.errors import OverfitError
from overfit.ingestion import chunker, loader, parser

app = typer.Typer(
    name="overfit",
    help="Turn course materials into structured, shareable study artifacts.",
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo("hello overfit")


CourseArg = Annotated[str, typer.Option("--course", "-c", help="Course folder name.")]
PathOpt = Annotated[
    Path | None,
    typer.Option("--path", "-p", help="Read from this directory instead of COURSES_DIR."),
]


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
            document = parser.parse(file, backend=settings.pdf_backend)
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
            document = parser.parse(file, backend=settings.pdf_backend)
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
            document = parser.parse(file, backend=settings.pdf_backend)
        except OverfitError as exc:
            typer.secho(f"  skip  {file.name}: {exc}", fg=typer.colors.RED)
            continue

        produced = chunker.chunk_document(
            document, settings.chunk_size, settings.chunk_overlap
        )
        crossing = sum(1 for c in produced if c.page_end)
        sizes = sorted(len(c.text) for c in produced)
        typer.secho(f"  {file.name}", fg=typer.colors.GREEN)
        typer.echo(
            f"      {len(document.pages)} pages -> {len(produced)} chunks"
            f"   |  chars min {sizes[0]} / median {sizes[len(sizes) // 2]} / max {sizes[-1]}"
            f"   |  {crossing} span a page break"
        )
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
    profile = IndexProfile(
        embed_model=embedder.model,
        embed_dim=embedder.dimension,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        parser=settings.pdf_backend,
    )
    store = VectorStore.open(
        settings.db_path(course), profile, course=course, create=create
    )
    return store, embedder, settings


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
    top_k: Annotated[int, typer.Option("--top-k", "-k", help="Results to show.")] = 5,
    chars: Annotated[int, typer.Option(help="Characters of each chunk to print.")] = 300,
) -> None:
    """Search the index directly, without generating anything.

    The fastest way to tell whether a disappointing answer is the retriever's
    fault or the model's. If the right material is not in this list, no
    prompt will rescue the output.
    """
    from overfit.query import retriever

    try:
        store, embedder, settings = _open_store(course)
        with store:
            hits = retriever.retrieve(query, store, embedder, top_k=top_k)
    except OverfitError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    typer.secho(f'\n"{query}"', fg=typer.colors.CYAN)
    typer.echo(f"{len(hits)} of {settings.top_k} requested\n")

    for rank, hit in enumerate(hits, start=1):
        typer.secho(
            f"{rank}. {hit.score:+.3f}  {hit.chunk.citation}",
            fg=typer.colors.GREEN if hit.score > 0.5 else typer.colors.WHITE,
        )
        text = " ".join(hit.chunk.text.split())
        typer.echo(f"   {text[:chars]}{'...' if len(text) > chars else ''}\n")


@app.command()
def mock(
    course: CourseArg,
    questions: Annotated[int, typer.Option("--questions", "-q", help="How many.")] = 10,
    topic: Annotated[str | None, typer.Option(help="Restrict to one subject.")] = None,
    material: Annotated[int, typer.Option(help="Passages to draw from.")] = 0,
) -> None:
    """Write a practice exam from the course material.

    Selects more passages than questions on purpose: some of what a course
    folder contains -- notices, acknowledgements, table fragments -- cannot
    support a question, and the model needs room to skip them rather than
    being forced to pad.
    """
    from overfit.query import generator, retriever

    settings = get_settings()
    settings.ensure_dirs()
    budget = material or questions * 2

    try:
        store, embedder, _ = _open_store(course)
        with store:
            chunks = retriever.gather_material(
                store, embedder, count=budget, topic=topic
            )
    except OverfitError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    writer = generator.Generator()
    typer.secho(f"\n{writer.model}", fg=typer.colors.CYAN)
    typer.echo(
        f"{len(chunks)} passages -> up to {questions} questions"
        f"{f'   (topic: {topic})' if topic else ''}"
    )
    # typer.echo(
    #     "A large local model loads ~17 GB before it writes a single token, so"
    #     " the first run is slow. Progress appears below once it starts.\n"
    # )

    import time as _time

    started = _time.monotonic()

    # The callback below only fires once tokens arrive, and nothing arrives
    # while the request is queued or the model is reasoning. Without this
    # line the terminal sits blank for that whole period, which is
    # indistinguishable from a hang -- and the reasonable response to a hang
    # is to kill it, throwing away work that was going fine.
    typer.echo("  request sent, waiting for the first token...", nl=False)
    first = True

    def tick(received: int, thinking: int = 0) -> None:
        nonlocal first
        if first:
            typer.echo("\r" + " " * 48 + "\r", nl=False)
            first = False
        elapsed = _time.monotonic() - started
        total = received + thinking
        rate = total / elapsed if elapsed else 0
        # Show reasoning separately: a model deep in its own monologue is
        # working, but it has not started answering, and those are different
        # states to be waiting in.
        stage = f"thinking {thinking}" if thinking and not received else f"{received} tokens"
        typer.echo(f"\r  {stage}   {elapsed:>5.0f}s   {rate:4.1f} tok/s ", nl=False)

    try:
        result = writer.mock_exam(
            course, chunks, count=questions, topic=topic, on_token=tick
        )
        typer.echo("\r" + " " * 48 + "\r", nl=False)
    except OverfitError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    items = result.exam.items
    if not items:
        typer.secho(
            "The model returned an explicitly empty list of questions.",
            fg=typer.colors.YELLOW,
        )
        typer.echo(
            "That can be honest -- the passages may be administrative or "
            "fragmentary -- but check the reply below before believing it.\n"
        )
        typer.echo(f"  raw response: {result.raw[:400] or '(empty body)'}\n")
        typer.echo("  passages it was given:")
        for chunk in chunks:
            preview = " ".join(chunk.text.split())[:90]
            typer.echo(f"    {chunk.citation}  {preview}")
        typer.echo(
            "\nIf those passages look examinable, the model is at fault: try "
            "--topic to focus it, or a different LLM_MODEL."
        )
        raise typer.Exit(0)

    paper, answers = generator.render_exam(course, result.exam)
    paper_path = settings.output_path(course, "mock_exam.md")
    answers_path = settings.output_path(course, "answers.md")
    paper_path.write_text(paper, encoding="utf-8")
    answers_path.write_text(answers, encoding="utf-8")

    typer.secho(f"{len(items)} questions", fg=typer.colors.GREEN)
    if len(items) < questions:
        typer.echo(
            f"  ({questions - len(items)} fewer than asked for -- the model "
            f"judged the rest of the material unsuitable, which is allowed)"
        )
    if result.dropped:
        typer.secho(
            f"  {len(result.dropped)} discarded for citing material that was "
            f"not supplied:",
            fg=typer.colors.YELLOW,
        )
        for note in result.dropped:
            typer.echo(f"    {note}")
    if result.attempts > 1:
        typer.echo(f"  took {result.attempts} attempts to satisfy the schema")

    typer.echo(f"\n  {paper_path}\n  {answers_path}")

    by_topic: dict[str, int] = {}
    for item in items:
        by_topic[item.topic] = by_topic.get(item.topic, 0) + 1
    typer.echo("\ntopics covered:")
    for name, number in sorted(by_topic.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {number:>3}  {name}")


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

    Computed entirely from the stored vectors -- no model call, nothing to
    hallucinate, every figure checkable against the files.
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

    shares = dict(
        (id(topic), share) for topic, share in selection.allocate(found, questions)
    )

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
    count: Annotated[int, typer.Option("--count", "-n", help="Chunks to select.")] = 12,
    topic: Annotated[str | None, typer.Option(help="Restrict to a topic first.")] = None,
    chars: Annotated[int, typer.Option(help="Characters of each chunk to print.")] = 180,
) -> None:
    """Preview the material a generation command would be given.

    This is the exam's syllabus before a single question exists. If the
    selection here misses half the unit, no prompt will produce a balanced
    paper -- and reading twelve chunks is far cheaper than reading ten
    questions and inferring what went wrong.
    """
    from overfit.query import retriever

    try:
        store, embedder, _ = _open_store(course)
        with store:
            total = store.stats()["chunks"]
            picked = retriever.gather_material(
                store, embedder, count=count, topic=topic
            )
    except OverfitError as exc:
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

    if report.furniture:
        typer.echo("        running furniture:")
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
