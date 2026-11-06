"""Proposal pre-checks (plan R1). Pure Python, no Claude."""

import pytest

from app.review import prechecks as pc
from app.review.prechecks import Limits, is_na, parse_rate, run_prechecks, self_focused_opener

GOOD = (
    "Your CRM drops leads after the form step. I rebuilt the same flow for a shop last year.\n"
    "Here is how I would fix it in two days."
)


def checks(findings):
    return [f["check"] for f in findings]


def only(findings, check):
    return [f for f in findings if f["check"] == check]


def test_clean_letter_has_no_problems():
    out = run_prechecks(GOOD, [], rate=30, budget={"min": 20, "max": 40, "job_type": "hourly"})
    assert [f for f in out if f["severity"] in ("error", "warn")] == []


@pytest.mark.parametrize("dash", ["—", "–", "―"])
def test_dash_is_an_error_with_span(dash):
    text = f"Leads leak {dash} we fix it."
    out = only(run_prechecks(text), "dash")
    assert len(out) == 1 and out[0]["severity"] == "error"
    span = out[0]["span"]
    assert span["field"] == "cover_letter"
    assert text[span["start"] : span["end"]] == dash


def test_dash_in_answer_names_the_answer():
    out = only(run_prechecks(GOOD, [{"question": "Q", "answer": "Yes — sure."}]), "dash")
    assert out[0]["span"]["field"] == "answer_1"
    assert "Answer 1" in out[0]["message"]


def test_hyphen_is_not_a_dash():
    assert only(run_prechecks("A well-known fix. 5-10 days."), "dash") == []


def test_space_before_punctuation():
    out = only(run_prechecks("Hello there , I can help ."), "space_before_punctuation")
    assert len(out) == 1 and "2 space" in out[0]["message"]
    assert only(run_prechecks("Fine. Really? Yes!"), "space_before_punctuation") == []


def test_space_before_dot_not_flagged_after_newline():
    assert only(run_prechecks("Line one.\n .hidden"), "space_before_punctuation") == []


@pytest.mark.parametrize(
    "text", ["Then i built it.", "i'm good at this.", "Yes. i will do it", "so i, too, agree"]
)
def test_lowercase_i_found(text):
    assert only(run_prechecks(text), "lowercase_i"), text


@pytest.mark.parametrize(
    "text", ["Use e.g. n8n, i.e. no code.", "Steps (i) and (ii).", "wi-fi is fine", "I did it."]
)
def test_lowercase_i_false_positives(text):
    assert only(run_prechecks(text), "lowercase_i") == [], text


def test_double_spaces():
    out = only(run_prechecks("Two  spaces here."), "double_space")
    assert len(out) == 1
    assert (
        only(run_prechecks("Line\n  indented is ok? no: only inside a line."), "double_space") == []
    )


def test_repeated_words():
    out = only(
        run_prechecks("I can fix the the flow. The Make make scenario too."), "repeated_word"
    )
    assert len(out) == 1 and "2 repeated words" in out[0]["message"]
    assert (
        only(run_prechecks("It said that that was fine. He had had enough."), "repeated_word") == []
    )


@pytest.mark.parametrize("text", ["NA", "N/A", "n/a", "n.a.", "  ", "", "None", "-"])
def test_is_na(text):
    assert is_na(text)


def test_is_na_real_answers():
    assert not is_na("I use n8n daily")
    assert not is_na("Nano agents")


def test_na_answer_is_an_error():
    out = run_prechecks(GOOD, [{"question": "Q1", "answer": "N/A"}], questions=["Q1"])
    assert only(out, "na_answer")[0]["severity"] == "error"
    assert only(out, "missing_answer") == []  # one finding, not two


def test_missing_answers_for_questions():
    out = run_prechecks(
        GOOD,
        [{"question": "Q1", "answer": "Done it twice."}, {"question": "Q2", "answer": "  "}],
        questions=["Q1", "Q2", "Q3"],
    )
    missing = only(out, "missing_answer")
    assert len(missing) == 2
    assert "question 2" in missing[0]["message"] and "question 3" in missing[1]["message"]


def test_no_questions_no_missing_check():
    assert only(run_prechecks(GOOD, [], questions=[]), "missing_answer") == []
    assert only(run_prechecks(GOOD, [], questions=None), "missing_answer") == []


def test_length_empty_warn_and_max():
    lim = Limits(length_warn=100, length_max=200)
    assert only(run_prechecks("", limits=lim), "length")[0]["severity"] == "error"
    warn = only(run_prechecks("x" * 150, limits=lim), "length")
    assert warn[0]["severity"] == "warn" and "150" in warn[0]["message"]
    hard = only(run_prechecks("x" * 250, limits=lim), "length")
    assert hard[0]["severity"] == "error" and "at most 200" in hard[0]["message"]
    assert only(run_prechecks("x" * 90 + " ok", limits=lim), "length") == []


def test_default_limits_are_1800_and_5000():
    assert only(run_prechecks("word " * 361), "length")[0]["severity"] == "warn"
    assert only(run_prechecks("word " * 1001), "length")[0]["severity"] == "error"


@pytest.mark.parametrize(
    "line",
    [
        "I am a developer",
        "My name is Sam",
        "Hi, I am Sam",
        "Hello, I'm Sam",
        "i build bots",
        "I’ve done this",
    ],
)
def test_self_focused_opener(line):
    assert self_focused_opener(line), line
    assert only(run_prechecks(line + ".\nSecond line is here for the client to read."), "opener")


@pytest.mark.parametrize(
    "line",
    [
        "Your CRM loses leads",
        "Imagine a flow that works",
        "Bots. Your forms break",
        "Hi Sam, your CRM leaks",
    ],
)
def test_client_focused_opener(line):
    assert not self_focused_opener(line), line


def test_first_two_lines_too_long_and_too_short():
    lim = Limits(first_lines_max=100, first_lines_min=20)
    long_line = "Your pipeline loses leads and " + "that costs money " * 10
    out = only(run_prechecks(long_line, limits=lim), "first_lines")
    assert out and out[0]["severity"] == "warn" and "under 100" in out[0]["message"]
    short = only(
        run_prechecks("Hi\nThere\nThird line is long enough to ignore here ok", limits=lim),
        "first_lines",
    )
    assert short and short[0]["severity"] == "info"
    assert only(run_prechecks(GOOD, limits=Limits()), "first_lines") == []


def test_first_two_lines_skips_blank_lines():
    assert pc.first_two_lines("\n\nOne\n\nTwo\nThree") == "One\nTwo"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("$35", 35.0),
        ("35.5/hr", 35.5),
        (40, 40.0),
        ("1,200", 1200.0),
        ("", None),
        ("abc", None),
        (None, None),
    ],
)
def test_parse_rate(value, expected):
    assert parse_rate(value) == expected


def test_rate_below_in_above():
    budget = {"min": 20, "max": 40, "job_type": "hourly", "kind": "client range"}
    below = only(run_prechecks(GOOD, rate=15, budget=budget), "rate")[0]
    above = only(run_prechecks(GOOD, rate=55, budget=budget), "rate")[0]
    inside = only(run_prechecks(GOOD, rate="$30/hr", budget=budget), "rate")[0]
    assert (
        below["severity"] == "warn"
        and "below" in below["message"]
        and "$20-$40/hr" in below["message"]
    )
    assert above["severity"] == "warn" and "above" in above["message"]
    assert inside["severity"] == "info" and "inside" in inside["message"]


def test_rate_with_no_range_or_default_range():
    assert "no budget range" in only(run_prechecks(GOOD, rate=30), "rate")[0]["message"]
    default = {"min": 11, "max": 50, "job_type": "hourly", "kind": "platform default"}
    msg = only(run_prechecks(GOOD, rate=30, budget=default), "rate")[0]["message"]
    assert "default range" in msg


def test_rate_missing_with_range_is_a_note():
    out = only(run_prechecks(GOOD, budget={"min": 20, "max": 40, "job_type": "hourly"}), "rate")
    assert out[0]["severity"] == "info" and "No rate" in out[0]["message"]


def test_fixed_job_uses_bid_words():
    out = only(
        run_prechecks(GOOD, rate=100, budget={"min": 300, "max": 300, "job_type": "fixed"}), "rate"
    )[0]
    assert "bid" in out["message"] and "$300" in out["message"] and "/hr" not in out["message"]


def test_instruction_like_start_word_is_not_checked_here():
    # "start with the word Bots" is a job instruction: the model checks it, not these rules.
    out = run_prechecks("Your CRM leaks leads. I fix that fast.\nTwo days of work.")
    assert not any("bots" in f["message"].lower() for f in out)


def test_counts():
    out = run_prechecks("A — b", [])
    c = pc.counts(out)
    assert c["error"] >= 1 and set(c) == {"error", "warn", "info"}


def test_limits_from_settings(config):
    from app.db import init_db
    from app.settings import Settings

    lim = pc.limits_from_settings(Settings(init_db(config.db_path)))
    assert (lim.length_warn, lim.length_max) == (1800, 5000)
