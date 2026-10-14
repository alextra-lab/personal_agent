"""FRE-1605: the sequential-thinking skill keeps only its procedure.

``REASONING_DISCIPLINE_PROMPT`` (FRE-1549) puts the reasoning rule in the system prompt on every
non-simple turn. The skill must not say the same rules again. It keeps the procedure that the
prompt does not hold: estimate the steps, state the current step, mark revisions, name branches,
check that the answer holds.

- AC-1: no sentence of the skill restates a rule of the prompt. A comparison test decides this,
  and a seeded negative (the restated rule put back) must fail it.
- AC-2: the five procedure steps stay in the body.
- AC-3: the description passes the A3 rules, and its token count is at most today's.

AC-4 (the 30-day load check) is a close-comment item. No code decides it.

The comparison finds shared wording. It does not find shared meaning. A rule restated in new
words that share no content words with the prompt passes it. Code review covers that case.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from personal_agent.orchestrator.prompts import REASONING_DISCIPLINE_PROMPT
from personal_agent.orchestrator.skills import _CHARS_PER_TOKEN, get_all_skills, validate_skill

_SKILL_DIR = Path(__file__).resolve().parents[3] / "skills" / "sequential-thinking"
_SKILL = "sequential-thinking"

# ── the comparison (AC-1) ─────────────────────────────────────────────────────────

_STOPWORDS = frozenset(
    "a an the and or of to in on at for with by is are was were be been it its that this these "
    "those as not no do does did you your when if then than so but from into out up any all each "
    "only just also which who what how why can will must may one two own same such too very has "
    "have had here there their them they before after while until about over under".split()
)
# A skill sentence restates a rule when it has one of two marks against one prompt sentence:
# this share of its content words is in the prompt sentence, or it holds this many of the
# prompt sentence's pairs of neighbouring content words.
_SHARED_WORDS = 0.6
_SHARED_PAIRS = 2
# A sentence with fewer content words is too short to compare.
_MIN_CONTENT_WORDS = 3
_SUFFIXES = ("ing", "ed", "es", "s", "ly")


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _content_words(sentence: str) -> list[str]:
    words = re.findall(r"[a-z]+", sentence.lower())
    return [_stem(w) for w in words if len(w) > 2 and w not in _STOPWORDS]


def _sentences(text: str) -> list[str]:
    """Split markdown into sentences: join wrapped lines, split bullets and table cells."""
    blocks: list[str] = []
    open_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            open_block = False
        elif open_block and not stripped.startswith(("-", "|", "#")):
            blocks[-1] += " " + stripped
        else:
            blocks.append(stripped)
            open_block = not stripped.startswith("#")
    sentences: list[str] = []
    for block in blocks:
        cleaned = re.sub(r"[`*#>]|^-\s+", "", block).replace("|", ". ")
        sentences += [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
    return sentences


def repeated_rules(text: str, prompt: str = REASONING_DISCIPLINE_PROMPT) -> list[str]:
    """Return each sentence of ``text`` that restates a sentence of ``prompt``."""
    rules = [_content_words(s) for s in _sentences(prompt)]
    found: list[str] = []
    for sentence in _sentences(text):
        words = _content_words(sentence)
        if len(set(words)) < _MIN_CONTENT_WORDS:
            continue
        pairs = set(zip(words, words[1:], strict=False))
        for rule in rules:
            shared_words = len(set(words) & set(rule)) / len(set(words))
            shared_pairs = len(pairs & set(zip(rule, rule[1:], strict=False)))
            if shared_words >= _SHARED_WORDS or shared_pairs >= _SHARED_PAIRS:
                found.append(sentence)
                break
    return found


def _real_text() -> str:
    """The catalog description and the body: everything the model can read of the skill."""
    doc = get_all_skills()[_SKILL]
    return f"{doc.description}\n\n{doc.body}"


# The "Rules" section of the skill on origin/main (468a72a3), before this ticket. It is the
# seeded negative: the restated rule that AC-1 must refuse.
_OLD_RULES_SECTION = """\
## Rules

Keep the discipline below internally on every turn this skill engages.
When the problem has a genuine false start, show the step you tried and
abandoned, alongside the correct path. Never invent a wrong step you did
not actually take. An unremarkable linear chain of reasoning with no false
start must stay invisible to the reader.
"""

_PROMPT_RULES = [
    s for s in _sentences(REASONING_DISCIPLINE_PROMPT) if len(set(_content_words(s))) >= 3
]


class TestAC1NoRepeatedRule:
    """AC-1: the skill states no rule of REASONING_DISCIPLINE_PROMPT again."""

    def test_the_skill_repeats_no_rule(self) -> None:
        assert repeated_rules(_real_text()) == []

    def test_the_seeded_old_rules_section_is_caught(self) -> None:
        caught = repeated_rules(_OLD_RULES_SECTION)
        assert "Never invent a wrong step you did not actually take." in caught
        assert any("linear chain of reasoning" in sentence for sentence in caught)

    def test_the_old_rules_section_put_back_into_the_real_skill_is_caught(self) -> None:
        restored = _real_text() + "\n\n" + _OLD_RULES_SECTION
        assert repeated_rules(restored)

    @pytest.mark.parametrize("rule", _PROMPT_RULES)
    def test_each_prompt_sentence_is_caught_when_put_into_the_skill(self, rule: str) -> None:
        assert repeated_rules(_real_text() + "\n\n" + rule) == [rule]

    @pytest.mark.parametrize(
        "paraphrase",
        [
            "Never make up a wrong step that you did not take. A made-up step is confabulation.",
            "Show a false start only when you really took one in your own reasoning.",
            "Keep the working private. A plain linear chain of reasoning stays invisible.",
        ],
    )
    def test_a_close_paraphrase_is_caught(self, paraphrase: str) -> None:
        assert repeated_rules(paraphrase)

    def test_the_comparison_leaves_the_procedure_alone(self) -> None:
        procedure = (
            "Before working the problem, form a rough count of the steps it will take. "
            "Know which step you are on and how many you believe remain."
        )
        assert repeated_rules(procedure) == []


# ── the procedure stays (AC-2) ────────────────────────────────────────────────────

_STEPS = [
    "Estimate first.",
    "State the current step.",
    "Mark a revision as a revision.",
    "Name a branch when you take one.",
    "Gate completion on done-ness.",
]


def _missing_steps(body: str) -> list[str]:
    """Return the procedure steps that are not a bold list item of ``body``, or ``["order"]``."""
    items = re.findall(r"^-\s+\*\*(.+?)\*\*", body, flags=re.MULTILINE)
    found = [item for item in items if item in _STEPS]
    missing = [step for step in _STEPS if step not in found]
    return missing or ([] if found == _STEPS else ["order"])


class TestAC2ProcedureKept:
    """AC-2: the five procedure steps stay in the body, in order."""

    def test_the_body_holds_the_five_steps(self) -> None:
        assert _missing_steps(get_all_skills()[_SKILL].body) == []

    @pytest.mark.parametrize("lost", _STEPS)
    def test_a_lost_step_is_reported(self, lost: str) -> None:
        body = get_all_skills()[_SKILL].body
        assert _missing_steps(body.replace(f"**{lost}**", lost)) == [lost]

    def test_steps_out_of_order_are_reported(self) -> None:
        body = get_all_skills()[_SKILL].body
        swapped = body.replace("**Estimate first.**", "**@@**").replace(
            "**State the current step.**", "**Estimate first.**"
        )
        assert _missing_steps(swapped.replace("**@@**", "**State the current step.**")) == ["order"]


# ── the catalog line (AC-3) ───────────────────────────────────────────────────────

# The description on origin/main (468a72a3) had 394 characters, so 98 tokens by the repo's
# estimate (``len // 4``). The catalog line adds the same name and "- " prefix to either text.
_TODAYS_DESCRIPTION_TOKENS = 98


class TestAC3CatalogLine:
    """AC-3: the description is valid under A3 and is not longer than before."""

    def test_the_skill_passes_every_validator_rule(self) -> None:
        assert validate_skill(_SKILL_DIR, "agent") == ()

    def test_the_not_for_clause_names_what_the_system_prompt_covers(self) -> None:
        description = get_all_skills()[_SKILL].description
        clause = description[description.index("Not for") :]
        assert "system prompt" in clause

    def test_the_description_is_at_most_todays_token_count(self) -> None:
        description = get_all_skills()[_SKILL].description
        assert len(description) // _CHARS_PER_TOKEN <= _TODAYS_DESCRIPTION_TOKENS

    def test_the_description_names_the_procedure(self) -> None:
        description = get_all_skills()[_SKILL].description.lower()
        for word in ("estimate", "revision", "branch"):
            assert word in description
