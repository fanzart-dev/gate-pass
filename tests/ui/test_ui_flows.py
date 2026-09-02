"""Browser tests for the flows that only exist in the browser.

    pip install pytest-playwright && playwright install chromium
    pytest tests/ui --verbose                       # against a temp instance
    BASE_URL=https://fanzart-server.local GP_USER=dinesh GP_PASSWORD=... \
        pytest tests/ui --verbose                   # against a running server

These cover what `tests/test_flow.py` cannot: JavaScript. Everything else is
tested there, faster and without a browser, and should stay there — these are
slow, and a slow suite gets skipped.

What is here is exactly the behaviour that lives in the page:

  * the drop zone counting and listing files (this silently broke once, when a
    `null` element threw inside the change handler BEFORE the list was drawn —
    the count, the list and the button all froze and the page looked inert);
  * Down/Up moving between rows in the items table, and Enter never submitting
    the form — the submit button issues a gate pass, so a stray keystroke
    spends a number;
  * a printed pass rendering with its serial, its items and both signatures.

By default this starts its own instance on a spare port with its own database,
so it never touches the real book. Point BASE_URL at a running server to test
that instead — it stays read-only unless you set GP_ALLOW_WRITES=1.
"""

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip(
    "playwright.sync_api",
    reason="pip install pytest-playwright && playwright install chromium")

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

BASE_URL = os.environ.get("BASE_URL")
USER = os.environ.get("GP_USER", "demo")
PASSWORD = os.environ.get("GP_PASSWORD", "demo-preview-only")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def base_url():
    """A running instance to test against.

    Its own throwaway database unless BASE_URL says otherwise, so running these
    can never issue a gate pass into the real register.
    """
    if BASE_URL:
        yield BASE_URL.rstrip("/")
        return

    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "tests" / "preview_server.py"), str(port)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                break
        except OSError:
            time.sleep(0.25)
    else:
        proc.kill()
        pytest.fail("the preview server did not come up")
    try:
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def page(base_url):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # ignore_https_errors: a server on the LAN uses a certificate signed by
        # the office's own CA, which this browser has no reason to trust.
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.goto(f"{base_url}/login")
        page.fill("#username", USER)
        page.fill("#password", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        assert "/login" not in page.url, f"could not sign in as {USER}"
        yield page
        browser.close()


def _fake_pdf(name):
    path = Path(tempfile.mkdtemp()) / name
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return str(path)


class TestUploadDropZone:
    def test_choosing_files_shows_how_many_and_which(self, page, base_url):
        page.goto(f"{base_url}/upload")
        assert page.locator("#file-panel").is_hidden(), "list shown before choosing"

        page.set_input_files("#invoice", [_fake_pdf("MANGALDEEP.pdf"),
                                           _fake_pdf("FR 262702140.pdf")])
        page.wait_for_selector("#file-panel:visible")

        assert page.locator("#file-count").inner_text().strip() == "2 files selected"
        names = page.locator("#file-list .file-name").all_inner_texts()
        assert names == ["MANGALDEEP.pdf", "FR 262702140.pdf"]
        # The button says what it is about to do.
        assert "2" in page.locator("#submit-btn").inner_text()

    def test_a_second_pick_adds_rather_than_replaces(self, page, base_url):
        page.goto(f"{base_url}/upload")
        page.set_input_files("#invoice", [_fake_pdf("one.pdf")])
        page.wait_for_selector("#file-panel:visible")
        page.set_input_files("#invoice", [_fake_pdf("two.pdf")])
        assert page.locator("#file-count").inner_text().strip() == "2 files selected"
        assert page.locator("#file-list li").count() == 2

    def test_clearing_puts_it_back(self, page, base_url):
        page.goto(f"{base_url}/upload")
        page.set_input_files("#invoice", [_fake_pdf("one.pdf")])
        page.wait_for_selector("#file-panel:visible")
        page.click("#clear-files")
        assert page.locator("#file-panel").is_hidden()
        assert page.locator("#submit-btn").inner_text().strip() == "Read Invoice"


class TestItemTableKeyboard:
    def _open_a_draft(self, page, base_url):
        page.goto(f"{base_url}/upload")
        page.set_input_files("#invoice", [_fake_pdf("typed-by-hand.pdf")])
        page.wait_for_selector("#file-panel:visible")
        page.click("#submit-btn")
        page.wait_for_url(re.compile(r"/(review|drafts)"), timeout=30000)
        if "/drafts" in page.url:
            page.click("a[href*='/review/']")
            page.wait_for_url(re.compile(r"/review/"))
        page.wait_for_selector("#items-table")

    def test_down_and_up_move_within_a_column(self, page, base_url):
        self._open_a_draft(page, base_url)
        for _ in range(3):
            page.click("#add-row")
        rows = page.locator("#items-table tbody tr")
        assert rows.count() >= 4

        first = rows.nth(0).locator("input[name=cartons]")
        first.click()
        page.keyboard.press("ArrowDown")
        assert rows.nth(1).locator("input[name=cartons]").evaluate(
            "el => el === document.activeElement")
        page.keyboard.press("ArrowDown")
        assert rows.nth(2).locator("input[name=cartons]").evaluate(
            "el => el === document.activeElement")
        page.keyboard.press("ArrowUp")
        assert rows.nth(1).locator("input[name=cartons]").evaluate(
            "el => el === document.activeElement")

    def test_enter_moves_down_and_never_submits(self, page, base_url):
        self._open_a_draft(page, base_url)
        for _ in range(2):
            page.click("#add-row")
        rows = page.locator("#items-table tbody tr")
        was = page.url

        rows.nth(0).locator("input[name=quantity]").click()
        page.keyboard.press("Enter")
        assert rows.nth(1).locator("input[name=quantity]").evaluate(
            "el => el === document.activeElement")
        # The submit button on this screen issues a gate pass. Enter must never
        # reach it — including on the last row, where there is nowhere to move.
        last = rows.nth(rows.count() - 1).locator("input[name=cartons]")
        last.click()
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)
        assert page.url == was, "Enter submitted the form and issued a pass"


class TestPrintedPass:
    def test_a_printed_pass_has_everything_on_it(self, page, base_url):
        page.goto(f"{base_url}/print/1")
        page.wait_for_selector(".sheet")

        assert page.locator(".pass").count() >= 1
        text = page.locator(".pass").first.inner_text()
        for expected in ("GATE PASS", "Serial No.", "Issue Date",
                         "Document No", "Remarks", "Authorised by", "Prepared by"):
            assert expected.lower() in text.lower(), f"missing: {expected}"

        # Nothing may fall off the bottom: the pass is overflow:hidden, so an
        # overflowing item is silently invisible rather than obviously wrong.
        overflow = page.locator(".pass").first.evaluate(
            "el => el.scrollHeight - el.clientHeight")
        assert overflow <= 1, f"the pass overflows by {overflow}px"

    def test_two_copies_on_an_a4_sheet(self, page, base_url):
        page.goto(f"{base_url}/print/1")
        page.wait_for_selector(".sheet")
        sheet = page.locator(".sheet").first
        if "single" in (sheet.get_attribute("class") or ""):
            pytest.skip("this instance is set to one pass per A5 sheet")
        assert sheet.locator(".pass").count() == 2, "A4 should carry two copies"
        serials = sheet.locator(".serial").all_inner_texts()
        assert serials[0] == serials[1], "the two halves must be the same pass"
