"""A real browser for the pages the platform serves.

Playwright's Python driver comes from the venv, the Chromium it drives from
`make browsers`, the system libraries from the devcontainer's playwright-deps
feature. Any of the three missing skips the browser tests instead of failing
them: they check the page, and a machine that cannot render one has told us
nothing about it.
"""
import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed (make build)")

from playwright.sync_api import Error, sync_playwright  # noqa: E402

TIMEOUT_MS = 10_000


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Error as e:
            pytest.skip(f"no browser to drive (make browsers): {str(e).splitlines()[0]}")

        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    """A fresh page per test, with the console and uncaught errors collected.

    Collected rather than raised so that a test can assert on them: a page that
    renders but logs an exception on every poll is broken in a way the DOM does
    not show.
    """
    context = browser.new_context()
    context.set_default_timeout(TIMEOUT_MS)
    page = context.new_page()
    page.errors = []
    page.on("pageerror", lambda error: page.errors.append(str(error)))

    yield page

    context.close()
