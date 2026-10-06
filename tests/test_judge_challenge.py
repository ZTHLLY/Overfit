"""Standalone challenge contracts and execution with local snapshots and fake clients only."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from overfit.evaluation import challenge, judge, runner

SUITE = Path(__file__).resolve().parents[1] / "eval/judge_challenges/ifn580_v1"


def make_setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    suite = tmp_path / "suite"
    suite.mkdir()
    for name in ("inputs.json", "annotations.json"):
        (suite / name).write_bytes((SUITE / name).read_bytes())
    settings = judge.JudgeSettings(
        base_url="https://fixture.invalid/v1",
        model="fixture-judge",
        api_key="SECRET_JUDGE_KEY",
        _env_file=None,
    )
    return NS(root=tmp_path, suite=suite, settings=settings)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    return make_setup(tmp_path, monkeypatch)


def response(body, finish="stop"):
    return NS(
        choices=[
            NS(
                message=NS(
                    content=json.dumps(body) if not isinstance(body, str) else body,
                    refusal=None,
                    reasoning_content="HIDDEN_REASONING",
                ),
                finish_reason=finish,
            )
        ],
        usage=NS(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="fixture-judge",
    )


class Fake:
    def __init__(self, actions=None):
        self.actions = iter(actions) if actions is not None else None
        self.calls = []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **request):
        self.calls.append(deepcopy(request))
        if self.actions is not None:
            action = next(self.actions)
            if isinstance(action, BaseException):
                raise action
            return action
        data = json.loads(request["messages"][1]["content"])
        chunks = data["materials"]
        if "answer" in data:
            chunk = next(
                c
                for c in chunks
                if c["source"] == data["citation"]["source"]
                and c["page"]
                <= data["citation"]["page"]
                <= (c["page_end"] or c["page"])
            )
            evidence = [{"chunk_id": chunk["id"]}]
            value = {
                "material_support": "supported",
                "citation_support": "supported",
                "reason": "PREVIOUS_RESPONSE",
                "claims": [
                    {
                        "text": "A fixture claim",
                        "verdict": "supported",
                        "evidence": evidence,
                    }
                ],
                "citation_evidence": evidence,
            }
        else:
            value = {
                "verdict": "answerable",
                "reason": "PREVIOUS_RESPONSE",
                "evidence": [{"chunk_id": chunks[0]["id"]}],
            }
        return response(value)


def execute(setup, fake=None, **kwargs):
    fake = fake or Fake()
    options = {
        "execute": True,
        "max_calls": 20,
        "settings": setup.settings,
        "client_factory": lambda _: fake,
        **kwargs,
    }
    return challenge.run_challenge(setup.suite, **options), fake


def rows(result):
    return [
        json.loads(line)
        for line in Path(result["challenge_dir"], "judgments.jsonl")
        .read_text()
        .splitlines()
    ]


def forbidden(*args, **kwargs):
    raise AssertionError("No configuration, service or provenance access allowed")


def edit_suite(setup, mutate):
    inputs = json.loads((setup.suite / "inputs.json").read_text())
    notes = json.loads((setup.suite / "annotations.json").read_text())
    mutate(inputs, notes)
    for name, value in (("inputs.json", inputs), ("annotations.json", notes)):
        (setup.suite / name).write_text(json.dumps(value))


def test_prepare_never_loads_configuration_client_or_source_provenance(
    setup, monkeypatch
):
    edit_suite(
        setup,
        lambda inputs, _: inputs["provenance"].update(
            source_tasks_path="/DO_NOT_OPEN/secret"
        ),
    )
    before = {p.name: p.read_bytes() for p in setup.suite.iterdir()}
    monkeypatch.setattr(judge, "JudgeSettings", forbidden)
    monkeypatch.setattr(judge, "make_client", forbidden)
    monkeypatch.setattr(judge, "invoke", forbidden)
    monkeypatch.setattr(runner, "_source", forbidden)
    result = challenge.run_challenge(setup.suite)
    assert result["status"] == "prepared" and result["metrics"]["call_starts"] == 0
    assert result["metrics"]["case_count"] == 7
    assert (
        result["metrics"]["control_count"] == 3
        and result["metrics"]["mutant_count"] == 4
    )
    assert result["metrics"]["planned_phase_count"] == 14
    assert all(
        row[phase]["reason"] == "prepare"
        for row in rows(result)
        for phase in challenge.PHASES
    )
    directory = Path(result["challenge_dir"])
    assert directory.parent == setup.root / "outputs/eval/challenges"
    assert {p.name: p.read_bytes() for p in setup.suite.iterdir()} == before
    assert all((directory / name).read_bytes() == raw for name, raw in before.items())
    assert "仅离线准备，没有调用模型" in (directory / "report.md").read_text()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda x, n: x.update(extra="hidden"),
        lambda x, n: x.update(schema="wrong"),
        lambda x, n: n.update(extra="hidden"),
        lambda x, n: n.update(suite_id="other"),
        lambda x, n: x["provenance"].update(extra="hidden"),
        lambda x, n: x["provenance"].update(source_tasks_sha256="bad"),
        lambda x, n: x["cases"][-1]["input"].update(page=True),
        lambda x, n: x["cases"][-1]["input"].update(page=17.0),
        lambda x, n: x["cases"][-1]["input"].update(page=9999),
        lambda x, n: x["cases"][-1]["input"].update(source="absent"),
        lambda x, n: x["cases"][-1]["input"].update(expected="unsupported"),
        lambda x, n: x["cases"][-1].update(case_id=x["cases"][0]["case_id"]),
        lambda x, n: n["cases"][-1].update(case_id=n["cases"][0]["case_id"]),
        lambda x, n: n["cases"].pop(),
        lambda x, n: x["chunks"][0].update(page=True),
        lambda x, n: x["chunks"][0].update(page_end=1),
        lambda x, n: x["chunks"][0].update(id=x["chunks"][1]["id"]),
        lambda x, n: x["chunks"][0].update(extra="hidden"),
        lambda x, n: n["cases"][0].update(control_case_id=n["cases"][1]["case_id"]),
        lambda x, n: n["cases"][3].update(control_case_id=n["cases"][4]["case_id"]),
        lambda x, n: n["cases"][3].update(source_item_id="wrong"),
        lambda x, n: n["cases"][3].update(changes=[]),
        lambda x, n: n["cases"][3]["changes"].append(
            deepcopy(n["cases"][3]["changes"][0])
        ),
        lambda x, n: n["cases"][3]["changes"][0].update(before="wrong"),
        lambda x, n: n["cases"][3]["changes"][0].update(after="wrong"),
        lambda x, n: n["cases"][5]["changes"][-1].update(before=True),
        lambda x, n: n["cases"][5]["changes"][-1].update(before=143.0),
        lambda x, n: n["cases"][5]["changes"][-1].update(before="143"),
        lambda x, n: n["cases"][3]["qualitative_target"].update(
            primary_dimensions=["invented"]
        ),
        lambda x, n: x["cases"][3]["input"].update(topic="changed"),
        lambda x, n: x["cases"][3]["input"].update(question="second factor"),
    ],
)
def test_entire_bad_suite_rejected_before_settings_even_when_unselected(
    setup, monkeypatch, mutate
):
    edit_suite(setup, mutate)
    monkeypatch.setattr(judge, "JudgeSettings", forbidden)
    monkeypatch.setattr(judge, "make_client", forbidden)
    with pytest.raises(challenge.ChallengeError):
        challenge.run_challenge(setup.suite, execute=True, max_calls=2, max_items=1)
    assert not (setup.root / "outputs").exists()


@pytest.mark.parametrize(
    "raw", ['{"schema":1,"schema":2}', "NaN", '{"x":Infinity}', "[]", "null", "\ufffd"]
)
def test_invalid_json_duplicate_keys_and_nonfinite_rejected(setup, raw):
    (setup.suite / "inputs.json").write_text(raw)
    with pytest.raises(challenge.ChallengeError):
        challenge.load_suite(setup.suite)


def test_reordered_annotations_are_valid(setup):
    edit_suite(setup, lambda _, notes: notes["cases"].reverse())
    result = challenge.run_challenge(setup.suite)
    assert result["status"] == "prepared"


def test_request_projection_blocks_design_siblings_and_previous_responses(setup):
    result, fake = execute(setup)
    inputs = json.loads((setup.suite / "inputs.json").read_text())
    assert len(fake.calls) == 14 and result["status"] == "completed"
    for index, request in enumerate(fake.calls):
        item = inputs["cases"][index // 2]["input"]
        data = json.loads(request["messages"][1]["content"])
        assert len(request["messages"]) == 2
        assert data["materials"] == [
            {k: c.get(k) for k in ("id", "source", "page", "page_end", "text")}
            for c in inputs["chunks"]
        ]
        assert data["question"] == item["question"]
        if index % 2:
            assert set(data) == {"question", "answer", "citation", "materials"}
            assert data["answer"] == item["answer"]
            assert data["citation"] == {"source": item["source"], "page": item["page"]}
        else:
            assert set(data) == {"question", "materials"}
        serialized = json.dumps(request)
        for token in (
            "case-00",
            "ifn580_v1",
            "source_item_id",
            "source_tasks_path",
            "qualitative_target",
            "design_reason",
            "PREVIOUS_RESPONSE",
            "control_case_id",
            '"topic"',
        ):
            assert token not in serialized
        for other in inputs["cases"]:
            if other["input"]["question"] != item["question"]:
                assert other["input"]["question"] not in data["question"]
    assert result["metrics"]["record_kind"] == "judge_challenge"
    assert not any(
        "retained" in key or "pass" in key or "source_run" in key
        for key in result["metrics"]
    )


def test_artifact_hashes_snapshots_unique_output_and_formal_replay_rejects(setup):
    result, _ = execute(setup, max_items=1)
    directory = Path(result["challenge_dir"])
    assert {p.name for p in directory.iterdir()} == challenge.ARTIFACTS | {
        "complete.json"
    }
    complete = json.loads((directory / "complete.json").read_text())
    manifest = json.loads((directory / "manifest.json").read_text())
    assert complete["record_kind"] == manifest["record_kind"] == "judge_challenge"
    for name, digest in complete["artifacts"].items():
        assert runner.digest((directory / name).read_bytes()) == digest
    for name, digest in manifest["snapshot_sha256"].items():
        assert (directory / name).read_bytes() == (setup.suite / name).read_bytes()
        assert runner.digest((directory / name).read_bytes()) == digest
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    other, _ = execute(setup, max_items=1, output_dir=setup.root / "custom/deep")
    assert Path(other["challenge_dir"]).parent == setup.root / "custom/deep"
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    with pytest.raises(runner.JudgeRunError, match="Unsupported"):
        runner.replay_judgment(directory)
    stored = "".join(raw.decode() for raw in before.values())
    assert "SECRET_JUDGE_KEY" not in stored and "HIDDEN_REASONING" not in stored


def test_selected_success_full_coverage_and_pair_design_not_synthetic_verdict(setup):
    result, fake = execute(setup, max_items=1, max_calls=2)
    metrics = result["metrics"]
    assert len(fake.calls) == 2 and result["exit_code"] == 0
    assert (
        metrics["selected_evaluated_phase_count"]
        == metrics["selected_phase_count"]
        == 2
    )
    assert (
        metrics["evaluated_phase_count"] == 2 and metrics["planned_phase_count"] == 14
    )
    assert all(
        r[p]["reason"] == "max_items"
        for r in rows(result)[1:]
        for p in challenge.PHASES
    )
    report = Path(result["report_path"]).read_text()
    assert (
        "Control / mutant 配对对照" in report and "人工设计目标（非模型结果）" in report
    )
    assert (
        "control 实际模型结果（未执行则未评）" in report and "not_evaluated" in report
    )
    assert "准确率=" not in report and "检测成功率=" not in report


@pytest.mark.parametrize(
    "first", [response("invalid"), TimeoutError("SECRET_PROVIDER")]
)
def test_schema_and_transient_retry_same_request_under_global_budget(setup, first):
    fixture = Fake()
    request = judge.build_request(
        "answerability",
        {"question": "Q"},
        json.loads((setup.suite / "inputs.json").read_text())["chunks"],
        setup.settings,
    )
    good = fixture.create(**request)
    fake = Fake([first, good])
    result, _ = execute(setup, fake, max_items=1, max_calls=2)
    assert result["exit_code"] == 1 and len(fake.calls) == 2
    assert fake.calls[0] == fake.calls[1]
    assert result["metrics"]["retry_count"] == 1
    assert rows(result)[0]["support"]["reason"] == "call_budget"
    assert (
        "SECRET_PROVIDER"
        not in Path(result["challenge_dir"], "calls.jsonl").read_text()
    )


@pytest.mark.parametrize(
    "a,s,pending", [("not_answerable", "unsupported", 0), ("unknown", "unknown", 1)]
)
def test_negative_unknown_not_retried_or_scored_against_design(setup, a, s, pending):
    fake = Fake(
        [
            response({"verdict": a, "reason": "r", "evidence": []}),
            response(
                {
                    "material_support": s,
                    "citation_support": s,
                    "reason": "r",
                    "claims": [],
                    "citation_evidence": [],
                }
            ),
        ]
    )
    result, _ = execute(setup, fake, max_items=1, max_calls=2)
    assert result["status"] == "completed" and len(fake.calls) == 2
    assert result["metrics"]["human_review_pending_case_count"] == pending
    assert result["metrics"]["evaluated_phase_count"] == 2
    assert result["metrics"]["model_all_positive_case_count"] == 0
    assert result["metrics"]["retry_count"] == 0


@pytest.mark.parametrize(
    "first,reason",
    [
        (response("{}", finish="length"), "truncated_response"),
        (RuntimeError("SECRET_PROVIDER"), "provider_error"),
    ],
)
def test_nonretryable_error_does_not_retry(setup, first, reason):
    result, fake = execute(setup, Fake([first]), max_items=1, max_calls=1)
    assert len(fake.calls) == 1 and result["exit_code"] == 1
    assert rows(result)[0]["answerability"]["reason"] == reason
    assert result["metrics"]["technical_error_phase_count"] == 1


def test_input_limit_skips_without_client_or_truncation(setup):
    setup.settings = setup.settings.model_copy(update={"max_input_chars": 1})
    result, _ = execute(setup, client_factory=forbidden)
    assert result["status"] == "incomplete" and result["metrics"]["call_starts"] == 0
    assert all(
        r[p]["reason"] == "input_limit" for r in rows(result) for p in challenge.PHASES
    )
    assert (
        Path(result["challenge_dir"], "inputs.json").read_bytes()
        == (setup.suite / "inputs.json").read_bytes()
    )


def test_interruption_stops_all_calls_and_persists_terminal(setup):
    result, fake = execute(setup, Fake([KeyboardInterrupt()]))
    assert (
        result["status"] == "interrupted"
        and result["exit_code"] == 130
        and len(fake.calls) == 1
    )
    assert all(
        r[p]["reason"] == "interrupted" for r in rows(result) for p in challenge.PHASES
    )
    complete = json.loads(Path(result["challenge_dir"], "complete.json").read_text())
    assert complete["status"] == "interrupted"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("inputs.json", 0),
        ("annotations.json", 0),
        ("manifest.json", 0),
        ("calls.jsonl", 0),
        ("judgments.jsonl", 2),
        ("review_queue.jsonl", 2),
        ("metrics.json", 2),
        ("report.md", 2),
        ("complete.json", 2),
    ],
)
def test_atomic_write_failure_never_claims_terminal_success(
    setup, monkeypatch, name, expected
):
    original = challenge.os.replace

    def fail(src, dst):
        if Path(dst).name == name:
            raise OSError("SECRET_DISK")
        return original(src, dst)

    monkeypatch.setattr(challenge.os, "replace", fail)
    fake = Fake()
    with pytest.raises(challenge.ChallengeError) as error:
        execute(setup, fake, max_items=1)
    assert "SECRET" not in str(error.value) and len(fake.calls) == expected
    directory = next((setup.root / "outputs/eval/challenges").iterdir())
    assert not (directory / "complete.json").exists()


@pytest.mark.parametrize(
    "kind,expected", [("call_started", 0), ("call_finished", 1), ("task_finished", 1)]
)
def test_event_fsync_failure_stops_model_calls(setup, monkeypatch, kind, expected):
    original = challenge.os.fsync

    def fail(fd):
        for path in (setup.root / "outputs/eval/challenges").glob("*/calls.jsonl"):
            if path.exists() and challenge.os.fstat(fd).st_ino == path.stat().st_ino:
                text = path.read_text()
                if text and json.loads(text.splitlines()[-1])["type"] == kind:
                    raise OSError("SECRET_DISK")
        return original(fd)

    monkeypatch.setattr(challenge.os, "fsync", fail)
    fake = Fake()
    with pytest.raises(challenge.ChallengeError):
        execute(setup, fake)
    assert len(fake.calls) == expected


def test_intent_is_durable_before_client_create(setup):
    fake = Fake()
    original = fake.create

    def check(**request):
        path = next((setup.root / "outputs/eval/challenges").glob("*/calls.jsonl"))
        last = json.loads(path.read_text().splitlines()[-1])
        assert last["type"] == "call_started" and last["request"] == request
        assert last["record_kind"] == "judge_challenge" and "item_id" not in last
        return original(**request)

    fake.chat.completions.create = check
    result, _ = execute(setup, fake, max_items=1)
    assert result["status"] == "completed"


def test_report_escapes_untrusted_design_and_model_text(setup):
    poison = "# forged\n<script>x</script>\n[click](https://bad.invalid)\n```"
    edit_suite(setup, lambda _, notes: notes["cases"][3].update(design_reason=poison))
    result = challenge.run_challenge(setup.suite)
    for line in Path(result["report_path"]).read_text().splitlines():
        if any(value in line for value in ("forged", "<script>", "[click]", "```")):
            assert line.startswith("    ")


def test_schema_retries_exhaust_fixed_attempts_and_keep_all_remaining_cases(setup):
    fake = Fake([response("bad"), response("bad")])
    result, _ = execute(setup, fake, max_calls=2)
    assert result["exit_code"] == 1 and len(fake.calls) == 2
    assert result["metrics"]["retry_count"] == 1
    assert rows(result)[0]["answerability"]["reason"] == "schema_error"
    assert rows(result)[0]["answerability"]["model_review"] is None
    assert rows(result)[0]["answerability"]["raw_response"] == "bad"
    assert len(rows(result)) == 7
    assert all(
        r[p]["reason"] == "call_budget"
        for r in rows(result)[1:]
        for p in challenge.PHASES
    )


def test_human_pending_never_changes_complete_model_verdict_or_retries(setup):
    data = json.loads((setup.suite / "inputs.json").read_text())
    chunk = data["chunks"][2]
    answer = {
        "verdict": "answerable",
        "reason": "Please inspect the ambiguity.",
        "evidence": [{"chunk_id": chunk["id"]}],
        "human_review_required": True,
    }
    support = {
        "material_support": "supported",
        "citation_support": "supported",
        "reason": "r",
        "claims": [
            {
                "text": "a",
                "verdict": "supported",
                "evidence": [{"chunk_id": chunk["id"]}],
            }
        ],
        "citation_evidence": [{"chunk_id": chunk["id"]}],
    }
    result, fake = execute(
        setup, Fake([response(answer), response(support)]), max_items=1
    )
    assert result["status"] == "completed" and len(fake.calls) == 2
    assert result["metrics"]["model_all_positive_case_count"] == 1
    assert result["metrics"]["human_review_pending_case_count"] == 1
    queue = [
        json.loads(line)
        for line in Path(result["challenge_dir"], "review_queue.jsonl")
        .read_text()
        .splitlines()
    ]
    assert queue[0]["case_id"] == data["cases"][0]["case_id"]
    assert queue[0]["model_review"] == answer and queue[0]["status"] == "pending"
    assert queue[0]["record_kind"] == "judge_challenge" and "item_id" not in queue[0]


def test_suite_files_and_output_parent_symlinks_rejected(setup):
    original = setup.suite / "inputs.json"
    moved = setup.root / "moved-inputs.json"
    original.rename(moved)
    original.symlink_to(moved)
    with pytest.raises(challenge.ChallengeError, match="symlinks"):
        challenge.load_suite(setup.suite)
    original.unlink()
    moved.rename(original)
    output = setup.root / "output-link"
    output.symlink_to(setup.root, target_is_directory=True)
    with pytest.raises(challenge.ChallengeError, match="symlink"):
        challenge.run_challenge(setup.suite, output_dir=output)


def test_source_provenance_not_opened_even_in_execution(setup, monkeypatch):
    sentinel = "/private/secret-provenance-must-not-be-opened"
    edit_suite(
        setup, lambda inputs, _: inputs["provenance"].update(source_tasks_path=sentinel)
    )
    original = Path.read_bytes

    def safe_read(path, *args, **kwargs):
        assert str(path) != sentinel
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", safe_read)
    result, _ = execute(setup, max_items=1)
    assert result["status"] == "completed"


def test_client_initialization_failure_is_safe_and_has_no_intent(setup):
    def fail(_):
        raise RuntimeError("SECRET_TOKEN")

    with pytest.raises(challenge.ChallengeError, match="client initialization failed"):
        execute(setup, client_factory=fail)
    directory = next((setup.root / "outputs/eval/challenges").iterdir())
    assert (directory / "calls.jsonl").read_text() == ""
    assert not (directory / "complete.json").exists()


def test_selected_mutant_does_not_auto_execute_unselected_control(setup):
    def reorder(inputs, _):
        inputs["cases"] = [
            inputs["cases"][3],
            *inputs["cases"][:3],
            *inputs["cases"][4:],
        ]

    edit_suite(setup, reorder)
    result, fake = execute(setup, max_items=1, max_calls=2)
    output = rows(result)
    assert len(fake.calls) == 2 and result["status"] == "completed"
    assert output[0]["case_id"] == "case-004"
    control = next(row for row in output if row["case_id"] == "case-001")
    assert all(control[phase]["reason"] == "max_items" for phase in challenge.PHASES)
    assert "answerability=not_evaluated" in Path(result["report_path"]).read_text()
