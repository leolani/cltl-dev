"""The manual front end: the same topologies, driven by a person.

Everything else in this suite asserts against the modules' own APIs. That leaves
one thing uncovered — the part of cltl-chat-ui a person actually uses. The
automatic tests drive its REST endpoints and never load its page, so the HTML,
the JavaScript and the static assets inside the image are tested by nobody.

These tests are `manual`-marked and excluded from `test`, `test-compose` and
`test-all`. They run only via `make test-manual`, they require a terminal, and
they ask a question whose answer only a person has. Each one also cross-checks
that answer against what the service recorded, so a distracted "y" cannot make
a broken chat UI look fine.
"""
import sys
import textwrap

import pytest

NEEDS_TERMINAL = (
    "manual tests need a terminal and pytest's capture turned off: "
    "`make test-manual`, or `pytest -s -m manual`")


def pytest_collection_modifyitems(config, items):
    """Skip rather than hang when there is nobody to answer.

    A manual test collected by accident — in CI, or from an editor — would block
    on ``input()`` until something killed it, which looks exactly like the hang
    that pytest's global timeout exists to diagnose.
    """
    if config.getoption("capture") == "no" and sys.stdin.isatty():
        return

    skip = pytest.mark.skip(reason=NEEDS_TERMINAL)
    for item in items:
        if item.get_closest_marker("manual"):
            item.add_marker(skip)


@pytest.fixture
def ask():
    """Put a question to the person running the tests, and take their answer."""
    def _ask(question: str) -> bool:
        print()
        print(textwrap.indent(textwrap.dedent(question).strip(), "  "))
        print()
        answer = input("  Did that work? [y/N] ").strip().lower()

        return answer in ("y", "yes")

    return _ask
