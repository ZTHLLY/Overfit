"""The student-facing MCQ templates keep question and answer sections aligned."""

from overfit.models import GeneratedMCQExam
from overfit.query import generator
from tests.test_eval_cli import mcq_item


def test_display_layout_separates_questions_and_keeps_each_option_citation():
    first, second = mcq_item(), mcq_item(source="week2/notes.pdf", page=6)
    first["correct_option"] = "B"
    second["correct_option"] = "D"
    second["question"] = "Which observation follows from the notes?"
    second["options"]["D"]["page_end"] = 8
    exam = GeneratedMCQExam.model_validate({"items": [first, second]})

    paper, answers = generator.render_mcq_display(
        "COURSE", exam, answers_file="COURSE_answers.md", generated_on="2030-05-06",
    )
    for document in (paper, answers):
        assert "## Question 1\n\nWhich statement is supported?\n\n" in document
        assert "\n---\n\n## Question 2\n\nWhich observation follows from the notes?" in document
        assert document.count("\n---\n") == 1
        assert "2030-05-06" in document
    assert "COURSE_answers.md" in paper
    assert "Correct answer" not in paper and "Feedback" not in paper and "notes.pdf" not in paper
    assert "**Correct answer:** B. Option B" in answers
    assert "**Correct answer:** D. Option D" in answers
    for label in "ABCD":
        assert f"**{label}.** Option {label}\n\n" in paper
        assert f"**{label}.** Option {label}\n\nFeedback {label}.\n\n> Source:" in answers
    assert answers.count("Source:") == 8
    assert "week2/notes.pdf pp6-8" in answers
