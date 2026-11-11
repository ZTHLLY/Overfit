"""Independent holdout checks: local fixtures only, no model or network calls."""

import hashlib
import json
import socket
import sqlite3
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from overfit.errors import ExtractionError, OverfitError
from overfit.evaluation import judge, runner
from overfit.evaluation.contracts import TraceReadError
from overfit.evaluation.elastic import locate_citation
from overfit.evaluation.replay import inspect_run
from overfit.ingestion import chunker, parser, pipeline
from overfit.models import EmbeddedChunk, Page, ParsedDocument
from overfit.query.generator import _templates
from overfit.storage.store import VectorStore
from tests.test_eval_provenance import trace_with_chunk
from tests.test_eval_replay import fixture_run
from tests.test_storage import PROFILE, legacy_index


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Independent structure tests must remain offline")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def snapshot(directory):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.iterdir()
        if p.is_file()
    }


@pytest.mark.parametrize("backend", ["docling", "docling+formula"])
@pytest.mark.parametrize("package", ["docling-core", "torch", "transformers"])
def test_independent_each_docling_backend_tracks_dependency_versions(
    monkeypatch, backend, package
):
    versions = {package: "held-out-v1"}
    monkeypatch.setattr(
        pipeline.metadata, "version", lambda name: versions.get(name, "fixed-v0")
    )
    before = pipeline.processing_identity(backend, 173, 19)
    versions[package] = "held-out-v2"
    assert pipeline.processing_identity(backend, 173, 19) != before


def test_independent_closed_wal_legacy_read_creates_no_sidecars(tmp_path):
    path = tmp_path / "historical.db"
    legacy_index(path)
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    db.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["historical.db"]
    before = snapshot(tmp_path)
    with VectorStore.open(path, PROFILE) as store:
        assert store.all_embedded()[0].chunk.provenance is None
        assert store.search([1.0, 0.0], 1)[0].chunk.provenance is None
        assert snapshot(tmp_path) == before
    assert snapshot(tmp_path) == before


def test_independent_closed_wal_legacy_write_refusal_has_no_side_effects(tmp_path):
    path = tmp_path / "historical.db"
    legacy_index(path)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.close()
    before = snapshot(tmp_path)
    with pytest.raises(OverfitError, match="read-only"):
        VectorStore.open(path, PROFILE, create=True)
    assert snapshot(tmp_path) == before


def test_independent_uncheckpointed_legacy_wal_never_returns_stale_snapshot(tmp_path):
    path = tmp_path / "historical.db"
    legacy_index(path)
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE documents SET hash = 'committed-only-in-wal'")
        writer.commit()
        assert path.with_name(path.name + "-wal").stat().st_size > 0
        before = snapshot(tmp_path)
        with pytest.raises(OverfitError, match="WAL"):
            VectorStore.open(path, PROFILE)
        assert snapshot(tmp_path) == before
    finally:
        writer.close()


def held_out_page(page, text, count=43):
    original = parser._plain_page(page, text, count, "held-out/7")
    value = deepcopy(original.provenance)
    value["document_sha256"] = "b" * 64
    return replace(original, provenance=value)


def test_independent_mixed_pages_retain_known_blocks_without_claiming_complete_identity():
    first = held_out_page(21, "An independent premise continues into another physical page")
    last = Page(22, "where the historical source metadata was never recorded.")
    chunk = chunker.chunk_document(ParsedDocument("holdout.pdf", [first, last]), 250, 25)[0]
    assert chunk.provenance is not None
    assert chunk.provenance["document_sha256"] is None
    assert chunk.provenance["blocks"][0]["locations"][0]["page"] == 21
    assert chunk.page == 21 and chunk.page_end == 22
    assert chunk.provenance["blocks"][0]["text_span"][1] < len(chunk.text)


def test_independent_cleaning_audit_text_never_enters_either_model_request():
    sentinel = "AUDIT_ONLY_HELD_OUT_SECRET_NOT_IN_ACTUAL_MATERIAL"
    body = "A conditional statement has no converse unless a separate premise establishes it."
    page = replace(held_out_page(17, body + "\n" + sentinel), furniture=(sentinel,))
    cleaned = parser._clean([page])[0]
    chunk = chunker.chunk_document(ParsedDocument("holdout.pdf", [cleaned]), 250, 25)[0]
    assert sentinel in json.dumps(chunk.provenance)
    assert sentinel not in chunk.text
    generated = _templates().get_template("mock_prompt.j2").render(
        course="held-out", chunks=[chunk], count=1, topic=None
    )
    assert sentinel not in generated
    settings = judge.JudgeSettings(
        base_url="https://offline.example/v1", model="fake", api_key="fake", _env_file=None
    )
    item = {"question": "Does the converse follow?", "answer": "ANSWER_ISOLATED_SENTINEL",
            "source": "holdout.pdf", "page": 17}
    for phase in ("answerability", "support"):
        request = judge.build_request(phase, item, [asdict(chunk)], settings)
        assert sentinel not in json.dumps(request)
        if phase == "answerability":
            assert item["answer"] not in json.dumps(request)


def test_independent_audit_only_nearby_block_cannot_launder_actual_faraway_text():
    text = "The actual selected premise is on a distant physical page, not near the citation."
    chunk = asdict(chunker.chunk_document(
        ParsedDocument("holdout.pdf", [held_out_page(35, text)]), 250, 25
    )[0])
    audit = deepcopy(chunk["provenance"]["blocks"][0])
    audit.update(block_id="audit-only", text_span=None, mapping="coarse")
    audit["locations"][0]["page"] = 12
    chunk["provenance"]["blocks"].append(audit)
    result = locate_citation({"source": "holdout.pdf", "page": 12}, [chunk], chunk)
    assert result["status"] == "source_issue"
    assert result["code"] == "citation_outside_window"
    assert result["evidence_pages"] == [35]


def test_independent_unmapped_nonwhitespace_cannot_be_verified_by_nearby_substring():
    text = "Mapped introduction. Unmapped substantive conclusion with no recorded source."
    page = held_out_page(11, text)
    value = deepcopy(page.provenance)
    value["blocks"][0].update(text_span=[0, 20], block_span=[0, 20])
    page = replace(page, provenance=value)
    chunk = asdict(chunker.chunk_document(ParsedDocument("holdout.pdf", [page]), 250, 25)[0])
    result = locate_citation({"source": "holdout.pdf", "page": 12}, [chunk], chunk)
    assert result["status"] == "unverified"
    assert result["severity"] == "info"


@pytest.mark.parametrize("verdict", ["supported", "unsupported", "unknown"])
def test_independent_location_tolerance_never_rewrites_model_verdict(verdict):
    text = "Evidence on a neighboring page can establish location but is not an automatic verdict."
    chunk = asdict(chunker.chunk_document(
        ParsedDocument("holdout.pdf", [held_out_page(24, text)]), 250, 25
    )[0])
    item = {"question": "Is this claim justified?", "answer": "A candidate answer.",
            "source": "holdout.pdf", "page": 23}
    evidence = [{"chunk_id": chunk["id"]}]
    raw = {"material_support": verdict, "citation_support": verdict,
           "reason": "Held-out synthetic model conclusion.",
           "claims": [{"text": "A candidate answer.", "verdict": verdict,
                       "evidence": evidence}], "citation_evidence": evidence}
    result = judge.validate_review("support", json.dumps(raw), item, [chunk])
    assert result["model_review"] == raw
    assert result["human_review"]["status"] == ("pending" if verdict == "unknown" else "not_required")
    location = result["verification"]["evidence_records"][-1]["elastic_location"]
    assert location["status"] == "verified"


def test_independent_long_figure_middle_chunks_carry_context_to_both_requests():
    text = (
        "[Figure text; spatial relationships not reconstructed]\n\n"
        + "Legend value and axis annotation with no standalone conclusion. " * 70
        + "\n\n[End figure text]"
    )
    page = held_out_page(32, text)
    value = deepcopy(page.provenance)
    block = value["blocks"][0]
    block.update(context_id="#/pictures/independent-figure", parent_id="diagram-group")
    sentinel = "AUDIT_ONLY_NOT_SELECTED_PREFIX "
    block["text"] = sentinel + text
    block["block_span"] = [len(sentinel), len(sentinel) + len(text)]
    block["locations"][0]["charspan"] = None
    page = replace(page, provenance=value)
    chunks = chunker.chunk_document(ParsedDocument("figure.pdf", [page]), 80, 9)
    middle = [c for c in chunks if "[Figure text" not in c.text and "[End figure" not in c.text]
    assert len(middle) > 3
    settings = judge.JudgeSettings(
        base_url="https://offline.example/v1", model="fake", api_key="fake", _env_file=None
    )
    for chunk in middle:
        prompt = _templates().get_template("mock_prompt.j2").render(
            course="held-out", chunks=[chunk], count=1, topic=None
        )
        assert "#/pictures/independent-figure" in prompt
        assert "Spatial relationships are not reconstructed" in prompt
        assert sentinel not in prompt
        item = {"question": "What can be inferred?", "answer": "Uncertain.",
                "source": "figure.pdf", "page": 32}
        for phase in ("answerability", "support"):
            request = judge.build_request(phase, item, [asdict(chunk)], settings)
            sent = json.dumps(request)
            assert "#/pictures/independent-figure" in sent
            assert "Spatial" in sent
            assert sentinel not in sent


def test_independent_source_changed_during_extraction_never_binds_new_digest(
    tmp_path, monkeypatch
):
    path = tmp_path / "changing.txt"
    old = "The initial file contains the original documented premise."
    path.write_text(old)

    def changed_source(path, backend):
        path.write_text("A different version was written while extraction was running.")
        return [parser._plain_page(1, old, None, "held-out/7")]

    monkeypatch.setattr(parser, "_read", changed_source)
    with pytest.raises(ExtractionError):
        parser.parse(path)


@pytest.mark.parametrize("target", ["manifest", "event"])
def test_independent_missing_trace_version_is_not_silently_current(tmp_path, target):
    writer = fixture_run(tmp_path)
    if target == "manifest":
        path = writer.run_dir / "manifest.json"
        value = json.loads(path.read_text())
        value.pop("schema_version")
        path.write_text(json.dumps(value))
        with pytest.raises(TraceReadError):
            inspect_run(writer.run_dir)
    else:
        path = writer.run_dir / "traces.jsonl"
        values = [json.loads(line) for line in path.read_text().splitlines()]
        values[1].pop("schema_version")
        path.write_text("".join(json.dumps(value) + "\n" for value in values))
        assert inspect_run(writer.run_dir)["trace_integrity"] == "incomplete"


@pytest.mark.parametrize(
    "anchor,pages,total", [(1, [1], 1), (61, [60, 61], 61), (29, [28, 30], 67)]
)
def test_postfreeze_coarse_table_at_edges_and_nonadjacent_pages(anchor, pages, total):
    """Fresh combinations after fixes; no real-PDF quality claim is implied."""
    text = "| Condition | Consequence |\n|---|---|\n| No independent premise | No converse |"
    page = held_out_page(pages[0], text, total)
    value = deepcopy(page.provenance)
    value["blocks"][0].update(
        label="table", mapping="coarse", parent_id="heldout-section",
        locations=[{"page": n, "charspan": [1200, 1300], "bbox": None} for n in pages]
    )
    page = replace(page, provenance=value)
    chunk = asdict(chunker.chunk_document(ParsedDocument("holdout-table.pdf", [page]), 250, 25)[0])
    result = locate_citation({"source": chunk["source"], "page": anchor}, [chunk], chunk)
    assert result["status"] == "verified"
    assert result["evidence_pages"] == pages
    assert result["window"] == [max(1, anchor - 1), min(total, anchor + 1)]
    assert "verdict" not in result
    assert chunk["provenance"]["blocks"][0]["locations"][0]["charspan"] == [1200, 1300]


def test_postfreeze_offline_snapshot_roundtrip_without_original_pdf(tmp_path):
    text = "A retained premise refers to both pages but does not imply its own converse."
    page = held_out_page(53, text, 71)
    value = deepcopy(page.provenance)
    value["blocks"][0].update(
        mapping="coarse",
        locations=[{"page": n, "charspan": None, "bbox": None} for n in (53, 54)]
    )
    page = replace(page, provenance=value)
    chunk = chunker.chunk_document(
        ParsedDocument("never-open-this-absent-file.pdf", [page]), 250, 25
    )[0]
    with VectorStore.open(tmp_path / "new.db", PROFILE, create=True) as store:
        store.add([EmbeddedChunk(chunk, [1.0, 0.0])])
        returned = store.search([1.0, 0.0], 1)[0].chunk
    assert returned.provenance == chunk.provenance
    writer, _ = trace_with_chunk(tmp_path, returned)
    assert inspect_run(writer.run_dir)["trace_integrity"] == "complete"
    prepared = runner.run_judge(writer.run_dir)
    from pathlib import Path
    directory = Path(prepared["judgment_dir"])
    before = snapshot(directory)
    metrics, _ = runner._reconstruct(directory)
    assert metrics["protocol_version"] == "judge-v4"
    assert metrics["run_status"] == "prepared"
    assert snapshot(directory) == before
