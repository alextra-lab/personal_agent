"""A joined option gets the same decision as the spaced form (FRE-1732 AC-2).

``mmdc -p/tmp/p.json`` passed the guard that refused ``mmdc -p /tmp/p.json``. The file names the
program that puppeteer starts. commander reads a cluster of short options, so ``-qp/tmp/p.json``
is the same option.

The other pairs are the refused options of the other guarded commands. They show which joined
forms the guard already handled.
"""

from __future__ import annotations

import pytest

from personal_agent.governance.permissions import decide_permission

_FILES = "-i /tmp/a.mmd -o /tmp/b.svg"


def _decision(command: str) -> str:
    return decide_permission("bash", {"command": command}, "NORMAL").decision


@pytest.mark.parametrize(
    "command",
    [
        f"mmdc -p/tmp/p.json {_FILES}",
        f"mmdc -qp/tmp/p.json {_FILES}",
        f"mmdc -qp /tmp/p.json {_FILES}",
        f"mmdc -fqp/tmp/p.json {_FILES}",
        f"mmdc --puppeteerConfigFile=/tmp/p.json {_FILES}",
        # commander reads a boolean short option, then "-" plus the rest, so these are long options.
        f"mmdc -q-puppeteerConfigFile=/tmp/p.json {_FILES}",
        f"mmdc -q-puppeteerConfigFile /tmp/p.json {_FILES}",
        f"mmdc -f-puppeteerConfigFile=/tmp/p.json {_FILES}",
        f"mmdc -fq-puppeteerConfigFile=/tmp/p.json {_FILES}",
        f"mmdc -qq-p/tmp/p.json {_FILES}",
    ],
)
def test_a_joined_puppeteer_option_asks(command: str) -> None:
    """Each form that the real mmdc reads as the option asks, as the spaced form does."""
    assert _decision(f"mmdc -p /tmp/p.json {_FILES}") == "ask"
    assert _decision(command) == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "mmdc -i /tmp/a.mmd -o/etc/b.svg",
        "mmdc -i /tmp/a.mmd -qo/etc/b.svg",
        "mmdc -i /tmp/a.mmd -qo /etc/b.svg",
        "mmdc -i /tmp/a.mmd --output=/etc/b.svg",
        "mmdc -i /tmp/a.md -a /etc/d",
        "mmdc -i /tmp/a.md -a/etc/d",
        "mmdc -i /tmp/a.md -qa/etc/d",
        "mmdc -i /tmp/a.md --artefacts=/etc/d",
        "mmdc -i /tmp/a.md --artefacts /etc/d",
        "mmdc -i /tmp/a.mmd -q-output=/etc/b.svg",
        "mmdc -i /tmp/a.mmd -f-artefacts=/etc/d",
    ],
)
def test_a_joined_output_outside_scratch_asks(command: str) -> None:
    """An output or artefacts path outside scratch asks in every form."""
    assert _decision(command) == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "mmdc -i /tmp/a.mmd -o /tmp/b.svg",
        "mmdc -i/tmp/a.mmd -o/tmp/b.svg",
        "mmdc -qi /tmp/a.mmd -o /tmp/b.svg",
        "mmdc -i /tmp/p.mmd -o /tmp/b.svg -q",
        "mmdc -t forest -i /tmp/a.mmd -o /tmp/b.svg",
        "mmdc -tforest -i /tmp/a.mmd -o /tmp/b.svg",
        "mmdc -b transparent -i /tmp/a.mmd -o /tmp/b.svg",
        "mmdc --help",
    ],
)
def test_a_plain_diagram_call_still_runs(command: str) -> None:
    """Seeded positive: a value holding the letter p is a value. The guard does not ask for it."""
    assert _decision(command) == "allow"


@pytest.mark.parametrize(
    ("spaced", "joined"),
    [
        ("sort -o /etc/x /tmp/a", "sort -o/etc/x /tmp/a"),
        ("sort -T /etc /tmp/a", "sort -T/etc /tmp/a"),
        ("sort --output /etc/x /tmp/a", "sort --output=/etc/x /tmp/a"),
        ("sort --compress-program x /tmp/a", "sort --compress-program=x /tmp/a"),
        ("rg --pre x y /tmp", "rg --pre=x y /tmp"),
        ("git diff --output /tmp/x", "git diff --output=/tmp/x"),
    ],
)
def test_the_other_guards_handle_the_joined_form(spaced: str, joined: str) -> None:
    """AC-2 report: these commands' refused options keep their decision when joined."""
    assert _decision(spaced) == "ask"
    assert _decision(joined) == "ask"


def test_an_unknown_short_option_asks() -> None:
    """Fail closed: a letter that the guard does not know can change how the rest is read."""
    assert _decision("mmdc -i /tmp/a.mmd -o /tmp/b.svg -Z") == "ask"
