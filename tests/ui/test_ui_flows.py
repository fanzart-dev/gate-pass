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
  * Enter moving ACROSS the items table and wrapping into the next row, the
    arrows moving down a column, and Enter never submitting the form — the
    submit button issues a gate pass, so a stray keystroke spends a number;
  * Remarks being a textarea that starts one line tall and takes Shift+Enter;
  * a printed pass rendering with its serial, its items and both signatures,
    no rule drawn between the two copies, and a multi-line remark whose lines
    all start after the colon rather than sliding back under the label.

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

    def test_enter_moves_across_the_row_and_never_submits(self, page, base_url):
        """Enter follows the line as it is read: item, quantity, cartons.

        It used to move DOWN, which meant typing a row needed the mouse or Tab
        between every cell.
        """
        self._open_a_draft(page, base_url)
        for _ in range(2):
            page.click("#add-row")
        rows = page.locator("#items-table tbody tr")
        was = page.url

        def focused(locator):
            return locator.evaluate("el => el === document.activeElement")

        rows.nth(0).locator("input[name=item_name]").click()
        page.keyboard.press("Enter")
        assert focused(rows.nth(0).locator("input[name=quantity]")), "item -> quantity"
        page.keyboard.press("Enter")
        assert focused(rows.nth(0).locator("input[name=cartons]")), "quantity -> cartons"

        # The end of a row falls into the start of the next one, so a whole
        # delivery is typed without ever leaving the keyboard.
        page.keyboard.press("Enter")
        assert focused(rows.nth(1).locator("input[name=item_name]")), \
            "the last column wraps to the next row"

        # The submit button on this screen issues a gate pass. Enter must never
        # reach it — including in the very last cell, where there is nowhere
        # left to move to.
        last = rows.nth(rows.count() - 1).locator("input[name=cartons]")
        last.click()
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)
        assert page.url == was, "Enter submitted the form and issued a pass"

    def test_arrows_still_move_straight_down_the_column(self, page, base_url):
        """The other way of working: one column at a time, down the delivery."""
        self._open_a_draft(page, base_url)
        for _ in range(3):
            page.click("#add-row")
        rows = page.locator("#items-table tbody tr")

        rows.nth(0).locator("input[name=quantity]").click()
        page.keyboard.press("ArrowDown")
        assert rows.nth(1).locator("input[name=quantity]").evaluate(
            "el => el === document.activeElement"), "Down keeps the column"
        page.keyboard.press("ArrowUp")
        assert rows.nth(0).locator("input[name=quantity]").evaluate(
            "el => el === document.activeElement"), "Up keeps the column"


class TestRemarks:
    def _open_a_draft(self, page, base_url):
        TestItemTableKeyboard._open_a_draft(self, page, base_url)

    def test_remarks_takes_several_lines_and_is_not_a_tall_empty_box(self, page, base_url):
        self._open_a_draft(page, base_url)
        remarks = page.locator("textarea[name=remarks]")
        assert remarks.count() == 1, "Remarks should be a textarea, not a one-line input"

        # It must start no taller than the single-line fields beside it. The
        # complaint was a two-line gap sitting in the middle of the form.
        height = remarks.evaluate("el => el.getBoundingClientRect().height")
        assert height < 56, f"Remarks box is {height}px tall before anything is typed"
        assert remarks.evaluate("el => getComputedStyle(el).resize") == "vertical"

        # Shift+Enter puts in a real newline rather than submitting.
        was = page.url
        remarks.click()
        page.keyboard.type("first line")
        page.keyboard.press("Shift+Enter")
        page.keyboard.type("second line")
        page.wait_for_timeout(200)
        assert page.url == was, "Shift+Enter submitted the form"
        assert remarks.input_value() == "first line\nsecond line"


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

    def test_no_line_is_drawn_between_the_two_copies(self, page, base_url):
        """The gap between the copies is white space, not a dashed rule.

        It used to draw a cut guide down the middle. On a printed document that
        line reads as part of the pass rather than as an instruction to whoever
        is holding the scissors.
        """
        page.goto(f"{base_url}/print/1")
        page.wait_for_selector(".sheet")
        gap = page.locator(".cut-line").first
        if gap.count() == 0:
            pytest.skip("one pass per sheet on this instance")
        drawn = gap.evaluate("""el => {
          const cs = getComputedStyle(el);
          return ['Top','Right','Bottom','Left']
            .filter(s => cs['border' + s + 'Style'] !== 'none'
                      && parseFloat(cs['border' + s + 'Width']) > 0);
        }""")
        assert drawn == [], f"a line is still drawn between the copies: {drawn}"

    def test_a_multi_line_remark_lines_up_after_the_colon(self, page, base_url):
        """Every line of a remark starts in the same place.

        Without the hanging indent the second and later lines slide back under
        the word "Remarks", which reads as a different field rather than a
        continuation of the same one.
        """
        page.goto(f"{base_url}/print/1")
        page.wait_for_selector(".sheet")
        body = page.locator(".remarks-body").first
        if body.count() == 0 or not body.inner_text().strip():
            pytest.skip("this pass has no remark to wrap")

        starts = body.evaluate("""el => {
          const range = document.createRange();
          const lefts = [];
          const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
          let node;
          while ((node = walk.nextNode())) {
            if (!node.textContent.trim()) continue;
            range.selectNodeContents(node);
            for (const r of range.getClientRects()) lefts.push(Math.round(r.left * 10) / 10);
          }
          return lefts;
        }""")
        assert starts, "no text found in the remark"
        assert max(starts) - min(starts) < 1.0, \
            f"remark lines start at different places: {starts}"

        # And the label must stay inside its own cell rather than hanging out
        # over the box next to it, which is what a negative text-indent did.
        placed = page.locator(".remarks-cell").first.evaluate("""td => {
          const label = td.querySelector('.remarks-label');
          return {cell: td.getBoundingClientRect().left,
                  label: label.getBoundingClientRect().left};
        }""")
        assert placed["label"] >= placed["cell"] - 0.5, \
            "the Remarks label is hanging outside its cell"

    def test_a_long_remark_cannot_push_the_pass_off_the_page(self, page, base_url):
        """However much is typed, the printed box stays two lines.

        The Remarks box shares a table row with the Document No / Date box,
        which is two lines tall. A third line grows the row, pushes the item
        table down and overflows the pass — and .pass is overflow:hidden, so
        the bottom of the last item row and part of the signature line simply
        disappear without a word. Found exactly that way, by adding a
        three-line remark to the preview data and watching the overflow check
        fail by 12px.
        """
        page.goto(f"{base_url}/print/1")
        page.wait_for_selector(".sheet")
        body = page.locator(".remarks-body").first
        if body.count() == 0:
            pytest.skip("no remarks box on this pass")

        page.evaluate("""() => {
          const el = document.querySelector('.remarks-body');
          el.innerHTML = ['Delivered to site gate by', 'FANZART LLP',
                          'received in good order', 'by the store keeper',
                          'and checked twice against the invoice'].join('<br>');
        }""")
        page.wait_for_timeout(50)

        height = body.evaluate("el => el.getBoundingClientRect().height")
        line = body.evaluate("el => parseFloat(getComputedStyle(el).lineHeight)")
        assert height <= line * 2 + 1, \
            f"the remarks box grew to {height}px, more than two {line}px lines"

        overflow = page.locator(".pass").first.evaluate(
            "el => el.scrollHeight - el.clientHeight")
        assert overflow <= 1, f"a long remark pushed the pass {overflow}px off the page"

    def test_two_copies_on_an_a4_sheet(self, page, base_url):
        page.goto(f"{base_url}/print/1")
        page.wait_for_selector(".sheet")
        sheet = page.locator(".sheet").first
        if "single" in (sheet.get_attribute("class") or ""):
            pytest.skip("this instance is set to one pass per A5 sheet")
        assert sheet.locator(".pass").count() == 2, "A4 should carry two copies"
        serials = sheet.locator(".serial").all_inner_texts()
        assert serials[0] == serials[1], "the two halves must be the same pass"
