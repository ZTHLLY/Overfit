"""Opt-in live/CLI integration with fake providers and temporary artifacts."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from overfit import cli
from overfit.config import EmbedSettings, LLMSettings, Settings
from overfit.errors import RerankError
from overfit.evaluation import live
from overfit.evaluation.contracts import TraceWriteError
from overfit.evaluation.trace import TraceWriter
from overfit.models import Chunk
from overfit.query import generator, retriever


def mcq_item(*, source="notes.pdf", page=5):
    return {
        "topic": "T",
        "question": "Which statement is supported?",
        "options": {
            label: {
                "text": f"Option {label}",
                "explanation": f"Feedback {label}.",
                "source": source,
                "page": page,
                "page_end": None,
            }
            for label in "ABCD"
        },
        "correct_option": "A",
    }


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        _env_file=None, llm=LLMSettings(_env_file=None, model="fixture", api_key="SECRET"),
        embed=EmbedSettings(_env_file=None, model="fixture-embed", api_key="SECRET"),
        outputs_dir=tmp_path / "outputs", index_dir=tmp_path / "index", max_retries=0,
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(generator, "get_settings", lambda: settings)
    monkeypatch.setattr(live, "collect_code_identity", lambda _: {"git_head": None, "dirty": None})
    monkeypatch.setattr(live, "read_index_identity", lambda _: {
        "path": "fixture.db", "meta": {}, "documents": [], "snapshot_sha256": None,
    })
    chunks = [Chunk("c1", "Synthetic course text", "notes.pdf", 5)]
    calls = []
    actions = []

    class Store:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            actions.append("closed")

    def open_store(course):
        records = list((settings.outputs_dir / "eval").glob("*/traces.jsonl"))
        assert records and json.loads(records[-1].read_text().splitlines()[0])["event_type"] == "run_started"
        actions.append("opened")
        return Store(), object(), settings

    monkeypatch.setattr(cli, "_open_store", open_store)
    monkeypatch.setattr(retriever, "gather_material", lambda *a, **kw: chunks)
    actual_generator = generator.Generator

    def use_response(response):
        def create(**kwargs):
            calls.append(kwargs)
            if isinstance(response, BaseException):
                raise response
            if not isinstance(response, str):
                return response
            return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=response))])])
        instance = actual_generator(settings.llm)
        instance._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        monkeypatch.setattr(generator, "Generator", lambda *args, **kwargs: instance)

    use_response(json.dumps({"questions": [mcq_item()]}))
    return SimpleNamespace(settings=settings, calls=calls, actions=actions, open_store=open_store,
                           use_response=use_response, chunks=chunks)


def run(setup, **kwargs):
    return live.run_mock_trace(setup.settings, setup.open_store, course="COURSE",
                               questions=1, topic=None, material=0, **kwargs)


def events(path):
    return [json.loads(line) for line in (Path(path) / "traces.jsonl").read_text().splitlines()]


def test_live_commits_complete_run_and_never_overwrites_course_files(setup):
    setup.settings.outputs_dir.mkdir()
    old = setup.settings.output_path("COURSE", "mock_exam.md")
    old.write_text("old exam")
    result = run(setup)
    assert result["status"] == "completed" and result["exit_code"] == 0
    run_dir = Path(result["run_dir"])
    assert old.read_text() == "old exam"
    assert (run_dir / "COURSE_mock_exam.md").exists()
    assert "COURSE_answers.md" in (run_dir / "COURSE_mock_exam.md").read_text()
    log = events(run_dir)
    assert log[-1]["event_type"] == "run_finished"
    assert log[-1]["payload"]["generation_outcome"] == "retained"
    assert log[-1]["payload"]["counts"]["call_count"] == len(setup.calls) == 1
    assert "SECRET" not in (run_dir / "manifest.json").read_text()
    assert setup.actions == ["opened", "closed"]
    second = run(setup)
    assert second["run_dir"] != result["run_dir"]


def test_topic_trace_records_actual_reranker_service_identity(setup):
    service = SimpleNamespace(
        adapter=SimpleNamespace(
            provider="openrouter",
            model="override/reranker",
            base_url="https://override.example/api/v1?token=SECRET",
        ),
        timeout=12.0,
    )

    result = live.run_mock_trace(
        setup.settings,
        setup.open_store,
        course="COURSE",
        questions=1,
        topic="clustering",
        material=0,
        reranker=service,
    )

    assert result["exit_code"] == 0
    manifest = json.loads((Path(result["run_dir"]) / "manifest.json").read_text())
    config = manifest["config"]
    assert config["rerank_provider"] == "openrouter"
    assert config["rerank_model"] == "override/reranker"
    assert config["rerank_endpoint"] == "https://override.example/api/v1"
    assert config["rerank_timeout"] == 12.0
    assert "SECRET" not in json.dumps(manifest)


def test_cli_topic_trace_uses_cli_built_service_identity(setup, monkeypatch):
    service = SimpleNamespace(
        adapter=SimpleNamespace(
            provider="openrouter",
            model="cli/reranker",
            base_url="https://cli.example/api/v1",
        ),
        timeout=9.0,
    )
    monkeypatch.setattr(cli, "_make_rerank_service", lambda *args, **kwargs: service)

    result = CliRunner().invoke(
        cli.app,
        [
            "mock",
            "--course",
            "COURSE",
            "--topic",
            "clustering",
            "--questions",
            "1",
            "--trace",
            "--rerank-model",
            "cli/reranker",
        ],
    )

    assert result.exit_code == 0, result.output
    run_dir = max((setup.settings.outputs_dir / "eval").iterdir())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    config = manifest["config"]
    assert config["rerank_model"] == "cli/reranker"
    assert config["rerank_endpoint"] == "https://cli.example/api/v1"
    assert config["rerank_timeout"] == 9.0
    assert manifest["task"]["questions"] == 1
    assert manifest["task"]["material_budget"] == 10


@pytest.mark.parametrize("raw,outcome", [
    ('{"questions": []}', "model_empty"),
    (json.dumps({"questions": [mcq_item(source="unknown.pdf")]}), "all_dropped"),
])
def test_empty_and_all_dropped_are_distinct_successes(setup, raw, outcome):
    setup.use_response(raw)
    result = run(setup)
    assert result["status"] == "empty" and result["exit_code"] == 0
    path = Path(result["run_dir"])
    assert not (path / "COURSE_mock_exam.md").exists()
    assert events(path)[-1]["payload"]["generation_outcome"] == outcome
    assert json.loads((path / "result.json").read_text())["generation_outcome"] == outcome


def test_initialization_failure_contacts_no_service(setup, monkeypatch):
    def fail(*args, **kwargs):
        raise TraceWriteError("SECRET failure")
    monkeypatch.setattr(TraceWriter, "create", fail)
    result = run(setup)
    assert result["exit_code"] == 1 and result["run_dir"] is None
    assert setup.actions == [] and setup.calls == []
    assert "SECRET" not in json.dumps(result)


def test_trace_rerank_failure_returns_only_safe_terminal_status(setup, monkeypatch):
    monkeypatch.setattr(
        retriever,
        "gather_material",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RerankError("SECRET provider failure", attempts=3)
        ),
    )

    result = run(setup)

    assert result["exit_code"] == 1
    assert result["rerank_failed"] is True
    assert result["rerank_attempts"] == 3
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("event_name,expected_calls", [("materials_selected", 0), ("call_started", 0), ("call_finished", 1)])
def test_mid_trace_failure_stops_further_calls(setup, monkeypatch, event_name, expected_calls):
    original = TraceWriter.emit
    def fail(self, event_type, payload):
        if event_type == event_name:
            self.failed = True
            raise TraceWriteError("fixture failure")
        return original(self, event_type, payload)
    monkeypatch.setattr(TraceWriter, "emit", fail)
    result = run(setup)
    assert result["exit_code"] == 1 and len(setup.calls) == expected_calls
    assert events(result["run_dir"])[-1]["event_type"] != "run_finished"


@pytest.mark.parametrize("error,status,code", [(RuntimeError("SECRET"), "failed", 1), (KeyboardInterrupt(), "interrupted", 130)])
def test_early_failure_has_no_materials_or_calls(setup, error, status, code):
    def fail(course):
        raise error
    setup.open_store = fail
    result = run(setup)
    assert result["status"] == status and result["exit_code"] == code
    log = events(result["run_dir"])
    assert [e["event_type"] for e in log] == ["run_started", "run_finished"]
    assert log[-1]["payload"]["generation_outcome"] == "not_started"
    assert "SECRET" not in json.dumps(log)
    assert not setup.calls


def test_interrupted_stream_records_partial_then_terminal(setup):
    def stream():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="partial"))])
        raise KeyboardInterrupt()
    setup.use_response(stream())
    result = run(setup)
    assert result["exit_code"] == 130
    log = events(result["run_dir"])
    finished = next(e["payload"] for e in log if e["event_type"] == "call_finished")
    assert finished["content"] == "partial" and finished["response_complete"] is False
    assert log[-1]["payload"]["status"] == "interrupted"
    assert len(setup.calls) == 1


@pytest.mark.parametrize("questions,material", [(0, 0), (-1, 0), (1, -1)])
def test_invalid_trace_arguments_stop_before_any_service(setup, questions, material):
    result = live.run_mock_trace(setup.settings, setup.open_store, course="COURSE",
                                 questions=questions, topic=None, material=material)
    assert result["exit_code"] == 1 and result["run_dir"] is None
    assert setup.calls == [] and setup.actions == []


def test_cli_trace_and_default_route_both_create_auditable_runs(setup, monkeypatch):
    result = CliRunner().invoke(cli.app, ["mock", "--course", "COURSE", "--trace"])
    assert result.exit_code == 0, result.output
    assert "Trace status: completed" in result.output
    eval_root = setup.settings.outputs_dir / "eval"
    run_dirs = list(eval_root.iterdir())
    assert len(run_dirs) == 1
    manifest = json.loads((run_dirs[0] / "manifest.json").read_text())
    assert manifest["task"]["questions"] == 30
    assert manifest["task"]["material_budget"] == 60
    assert not setup.settings.output_path("COURSE", "mock_exam.md").exists()
    assert not setup.settings.output_path("COURSE", "answers.md").exists()
    count = len(run_dirs)
    class Store:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
    monkeypatch.setattr(cli, "_open_store", lambda course: (Store(), object(), setup.settings))
    result = CliRunner().invoke(cli.app, ["mock", "--course", "COURSE"])
    assert result.exit_code == 0, result.output
    assert "Semantic uniqueness and distractor correctness: not judged" in result.output
    paper = setup.settings.output_path("COURSE", "mock_exam.md").read_text()
    answers = setup.settings.output_path("COURSE", "answers.md").read_text()
    assert "## Question 1" in paper and "## Question 1" in answers
    assert "**Correct answer:**" in answers
    [default_run] = set(eval_root.iterdir()) - set(run_dirs)
    manifest = json.loads((default_run / "manifest.json").read_text())
    assert manifest["started_at"][:10] in paper and manifest["started_at"][:10] in answers
    for directory in (run_dirs[0], default_run):
        assert "## Question 1" not in (directory / "COURSE_mock_exam.md").read_text()
        assert "## Question 1" not in (directory / "COURSE_answers.md").read_text()
    assert len(list(eval_root.iterdir())) == count + 1
    assert setup.calls[-1]["timeout"] == setup.settings.request_timeout


def test_settings_default_question_count_matches_mock_cli_default(setup):
    assert setup.settings.num_questions == 30


def test_mock_judge_is_explicit_and_additional(setup, monkeypatch):
    from overfit.evaluation import judge, mcq_protocols, runner

    calls = []

    judge_settings = object()
    monkeypatch.setattr(judge, "JudgeSettings", lambda: judge_settings)
    monkeypatch.setattr(mcq_protocols, "MCQJudgeV2Settings", lambda: pytest.fail("V2 settings loaded"))

    def fake_judge(run_dir, *, execute, settings):
        assert settings is judge_settings
        calls.append((Path(run_dir), execute))
        return {
            "status": "completed",
            "protocol_version": "mcq-judge-v1",
            "report_path": str(Path(run_dir) / "judge-report.md"),
            "exit_code": 0,
            "publication": None,
            "metrics": {
                "mode": "execute", "retained_count": 1,
                "selected_evaluated_phase_count": 1, "selected_phase_count": 1,
                "evaluated_phase_count": 1, "planned_phase_count": 1,
                "model_all_positive_count": 1,
                "human_review_pending_item_count": 0,
                "technical_error_phase_count": 0, "call_starts": 1,
                "response_count": 1, "retry_count": 0, "accepted_count": 1,
            },
        }

    monkeypatch.setattr(runner, "run_judge", fake_judge)
    result = CliRunner().invoke(
        cli.app, ["mock", "--course", "COURSE", "--questions", "1", "--judge"]
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1 and calls[0][1] is True
    assert "not judged" not in result.output
    assert "Judged publication retained 1 questions" in result.output
    assert "Protocol: mcq-judge-v1" in result.output
    assert "MCQ Judge v2 preflight" not in result.output
    assert not setup.settings.output_path("COURSE", "mock_exam.md").exists()
    assert not setup.settings.output_path("COURSE", "answers.md").exists()


@pytest.mark.parametrize("trace", [False, True])
def test_mock_judge_display_uses_published_order_and_judgment_date(setup, monkeypatch, trace):
    from overfit.evaluation import judge, runner
    from overfit.models import GeneratedMCQExam

    monkeypatch.setattr(judge, "JudgeSettings", object)
    first, second = mcq_item(), mcq_item()
    second["question"] = "What did the second approved example show?"
    setup.use_response(json.dumps({"questions": [first, second]}))
    audit_artifacts = {}
    publication_details = {}

    def fake_judge(run_dir, *, execute, settings):
        original = json.loads((run_dir / "result.json").read_text())["items"]
        assert len(original) == 2
        items = list(reversed(original))
        item = items[0]
        old = item["correct_option"]
        new = next(label for label in "ABCD" if label != old)
        item["correct_option"] = new
        item["options"][old], item["options"][new] = item["options"][new], item["options"][old]
        exam = GeneratedMCQExam.model_validate({"items": items})
        judgment = run_dir / "judgments" / "fixture"
        judgment.mkdir(parents=True)
        (judgment / "manifest.json").write_text(json.dumps({"created_at": "2042-01-02T00:00:00+00:00"}))
        paper, answers = generator.render_exam(
            "COURSE", exam, answers_file="judged_answers.md", generated_on="2042-01-02",
        )
        paper_file, answers_file = judgment / "judged_mock_exam.md", judgment / "judged_answers.md"
        paper_file.write_text(paper)
        answers_file.write_text(answers)
        audit_artifacts.update({paper_file: paper, answers_file: answers})
        publication_details.update(questions=[entry["question"] for entry in items], correct_label=new)
        return {
            "judgment_dir": str(judgment), "report_path": str(judgment / "report.md"),
            "status": "completed", "exit_code": 0, "protocol_version": "mcq-judge-v1",
            "publication": {"paper": str(paper_file), "answers": str(answers_file)},
            "accepted_items": items,
            "metrics": {
                "mode": "execute", "retained_count": 2, "accepted_count": 2,
                "selected_evaluated_phase_count": 2, "selected_phase_count": 2,
                "evaluated_phase_count": 2, "planned_phase_count": 2,
                "model_all_positive_count": 2, "human_review_pending_item_count": 0,
                "technical_error_phase_count": 0, "call_starts": 2, "response_count": 2,
                "retry_count": 0,
            },
        }

    monkeypatch.setattr(runner, "run_judge", fake_judge)
    args = ["mock", "--course", "COURSE", "--questions", "2", "--judge"]
    result = CliRunner().invoke(cli.app, args + (["--trace"] if trace else []))
    assert result.exit_code == 0, result.output
    paper = setup.settings.output_path("COURSE", "mock_exam.md")
    answers = setup.settings.output_path("COURSE", "answers.md")
    if trace:
        assert not paper.exists() and not answers.exists()
    else:
        paper_text, answers_text = paper.read_text(), answers.read_text()
        for number, question in enumerate(publication_details["questions"], start=1):
            assert f"## Question {number}\n\n{question}" in paper_text
        assert "2042-01-02" in paper_text and "2042-01-02" in answers_text
        assert "COURSE_answers.md" in paper_text and "judged_answers.md" not in paper_text
        first_answer, _ = answers_text.split("## Question 2", maxsplit=1)
        assert f"**Correct answer:** {publication_details['correct_label']}. Option A" in first_answer
    for path, content in audit_artifacts.items():
        assert path.read_text() == content and "## Question 1" not in content


def test_mock_judge_configuration_fails_before_generation(setup, monkeypatch):
    from overfit.evaluation import judge, live

    monkeypatch.setattr(
        judge,
        "JudgeSettings",
        lambda: (_ for _ in ()).throw(ValueError("invalid")),
    )
    contacted = False

    def forbidden(*args, **kwargs):
        nonlocal contacted
        contacted = True
        raise AssertionError("generation must remain cold")

    monkeypatch.setattr(live, "run_mock_trace", forbidden)
    result = CliRunner().invoke(
        cli.app, ["mock", "--course", "COURSE", "--questions", "1", "--judge"]
    )

    assert result.exit_code == 1
    assert "Invalid judge configuration" in result.output
    assert contacted is False and setup.calls == []


def test_eval_judge_mcq_defaults_to_v1_and_uses_its_settings(setup, monkeypatch):
    from overfit.evaluation import judge, mcq_protocols

    run_dir = Path(run(setup)["run_dir"])
    for name, value in {
        "JUDGE_BASE_URL": "https://judge.example/v1", "JUDGE_API_KEY": "fixture",
        "JUDGE_MODEL": "judge-fixture", "JUDGE_MAX_OUTPUT_TOKENS": "513",
        "JUDGE_MAX_ATTEMPTS": "3", "JUDGE_TEMPERATURE": "0.2",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(mcq_protocols, "MCQJudgeV2Settings", lambda: pytest.fail("V2 settings loaded"))
    calls = []

    def create(**request):
        calls.append(request)
        correct = json.loads(request["messages"][1]["content"])["item"]["correct_option"]
        review = {
            "human_review_required": False,
            "answerable": True, "one_best_answer": True, "correct_option_supported": True,
            "other_options_incorrect": {label: label != correct for label in "ABCD"},
            "explanations_accurate": dict.fromkeys("ABCD", True),
            "citations_supported": dict.fromkeys("ABCD", True),
            "positive_stem": True, "no_calculation": True, "reason": "Fixture review.",
        }
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(review)), finish_reason="stop")],
            usage=None, model="judge-fixture",
        )

    monkeypatch.setattr(judge, "make_client", lambda _: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    ))
    result = CliRunner().invoke(cli.app, ["eval", "judge", str(run_dir), "--execute"])
    assert result.exit_code == 0, result.output
    assert "Protocol: mcq-judge-v1" in result.output
    assert "MCQ Judge v2 preflight" not in result.output
    assert len(calls) == 1
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["max_tokens"] == 513
    assert calls[0]["temperature"] == 0.2
    [path] = (run_dir / "judgments").glob("*/manifest.json")
    manifest = json.loads(path.read_text())
    assert manifest["protocol_version"] == "mcq-judge-v1"
    assert manifest["max_calls"] == 3


def test_eval_judge_mcq_does_not_ignore_conflicting_call_budget(setup, monkeypatch):
    from overfit.evaluation import judge, mcq_protocols

    run_dir = Path(run(setup)["run_dir"])

    def forbidden(*args, **kwargs):
        pytest.fail("Conflicting MCQ call budget must be rejected before settings/client")

    monkeypatch.setattr(judge, "JudgeSettings", forbidden)
    monkeypatch.setattr(mcq_protocols, "MCQJudgeV2Settings", forbidden)
    monkeypatch.setattr(judge, "make_client", forbidden)
    result = CliRunner().invoke(cli.app, ["eval", "judge", str(run_dir), "--execute", "--max-calls", "1"])
    assert result.exit_code == 1
    assert "--max-calls" in result.output
    assert "JUDGE_MAX_ATTEMPTS" in result.output
    assert not (run_dir / "judgments").exists()


@pytest.mark.parametrize("failed_artifact", [
    "result.json", "report.md", "COURSE_mock_exam.md", "COURSE_answers.md",
])
def test_artifact_write_failure_records_failed_terminal_when_log_writable(
    setup, monkeypatch, failed_artifact,
):
    from overfit.evaluation.replay import inspect_run

    original = TraceWriter._write_atomic
    def fail(self, name, content):
        if name == failed_artifact:
            raise OSError("fixture disk failure")
        return original(self, name, content)
    monkeypatch.setattr(TraceWriter, "_write_atomic", fail)
    result = run(setup)
    assert result["exit_code"] == 1
    assert len(setup.calls) == 1
    terminal = events(result["run_dir"])[-1]
    assert terminal["event_type"] == "run_finished"
    assert terminal["payload"]["status"] == "failed"
    assert terminal["payload"]["stage"] == "artifacts"
    assert failed_artifact not in terminal["payload"]["artifacts"]
    replay = inspect_run(Path(result["run_dir"]))
    assert replay["run_status"] == "failed" and replay["trace_integrity"] == "complete"
    assert replay["generation_outcome"] == "retained"


def test_read_index_identity_uses_document_manifest_not_snapshot(tmp_path):
    path = tmp_path / "test.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        db.execute("CREATE TABLE documents (source TEXT, hash TEXT)")
        db.execute("INSERT INTO meta VALUES ('parser', 'fixture')")
        db.execute("INSERT INTO documents VALUES ('a.pdf', 'digest')")
    identity = live.read_index_identity(path)
    assert identity["meta"] == {"parser": "fixture"}
    assert identity["documents"] == [{"source": "a.pdf", "hash": "digest"}]
    assert identity["snapshot_sha256"] is None
    assert len(identity["document_manifest_sha256"]) == 64
    assert identity == live.read_index_identity(path)


def test_generation_exhaustion_is_failed_not_model_empty(setup):
    setup.use_response(RuntimeError("SECRET provider error"))
    result = run(setup)
    assert result["status"] == "failed" and result["exit_code"] == 1
    assert len(setup.calls) == 3
    terminal = events(result["run_dir"])[-1]["payload"]
    assert terminal["generation_outcome"] == "failed" and terminal["stage"] == "generation"
    assert "SECRET" not in json.dumps(events(result["run_dir"]))


def test_invalid_material_metadata_is_not_silently_repaired(setup):
    setup.chunks[:] = [Chunk("c1", "text", "notes.pdf", 5, page_end=3)]
    result = run(setup)
    assert result["exit_code"] == 1 and not setup.calls
    log = events(result["run_dir"])
    assert [e["event_type"] for e in log] == ["run_started", "run_finished"]
    assert log[-1]["payload"]["stage"] == "selection"


def test_terminal_persistence_error_is_nonzero_even_if_marker_is_readable(setup, monkeypatch):
    original = TraceWriter.emit
    def fail_after_write(self, event_type, payload):
        original(self, event_type, payload)
        if event_type == "run_finished":
            self.failed = True
            raise TraceWriteError("Simulated late fsync confirmation failure")
    monkeypatch.setattr(TraceWriter, "emit", fail_after_write)
    result = run(setup)
    assert result["exit_code"] == 1 and result["status"] == "failed"
    assert events(result["run_dir"])[-1]["payload"]["status"] == "completed"
    assert len(setup.calls) == 1


def test_cli_replay_is_offline_and_does_not_modify_originals(setup, monkeypatch):
    result = run(setup)
    run_dir = Path(result["run_dir"])
    originals = {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}
    def forbidden(*args, **kwargs):
        raise AssertionError("Replay must not load settings, models, or indexes")
    monkeypatch.setattr(cli, "get_settings", forbidden)
    monkeypatch.setattr(cli, "_open_store", forbidden)
    monkeypatch.setattr(generator, "Generator", forbidden)
    monkeypatch.setattr(generator, "get_settings", forbidden)
    monkeypatch.setattr(live, "read_index_identity", forbidden)
    replay = CliRunner().invoke(cli.app, ["eval", "replay", str(run_dir)])
    assert replay.exit_code == 0, replay.output
    assert "completed / complete" in replay.output
    assert {name: (run_dir / name).read_bytes() for name in originals} == originals
    assert len(setup.calls) == 1
    report = json.loads(next((run_dir / "replays").glob("*/result.json")).read_text())
    assert report["semantic_status"] == "not_evaluated"
    assert report["original_cli_exit_status"] is None


def test_cli_replay_incomplete_trace_returns_nonzero(setup):
    result = run(setup)
    run_dir = Path(result["run_dir"])
    log = run_dir / "traces.jsonl"
    log.write_text("\n".join(log.read_text().splitlines()[:-1]) + "\n")
    replay = CliRunner().invoke(cli.app, ["eval", "replay", str(run_dir)])
    assert replay.exit_code == 1
    assert "unknown / incomplete" in replay.output


def test_cli_replay_complete_early_failure_is_not_run_success(setup):
    def failure(course):
        raise RuntimeError("fixture")
    setup.open_store = failure
    result = run(setup)
    replay = CliRunner().invoke(cli.app, ["eval", "replay", result["run_dir"]])
    assert replay.exit_code == 0, replay.output
    assert "failed / complete" in replay.output


@pytest.mark.parametrize("provider_result,status,outcome", [
    (RuntimeError("synthetic exhaustion"), "failed", "failed"),
    (KeyboardInterrupt(), "interrupted", None),
])
def test_terminal_generation_failure_and_interrupt_replay_distinguish_unknown(
    setup, provider_result, status, outcome,
):
    from overfit.evaluation.replay import inspect_run

    setup.use_response(provider_result)
    result = run(setup)
    assert result["status"] == status
    replay = inspect_run(Path(result["run_dir"]))
    assert replay["trace_integrity"] == "complete", replay["problems"]
    assert replay["run_status"] == status
    assert replay["generation_outcome"] == outcome
    terminal = events(result["run_dir"])[-1]["payload"]
    assert terminal["generation_outcome"] == outcome
