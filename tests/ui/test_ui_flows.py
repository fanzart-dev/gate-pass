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
  * the running quantity and carton totals following every keystroke, added row
    and removed row — they exist only in the browser, so only a browser can
    check them;
  * the Drafts table fitting its container at several widths, which depends on
    font metrics and so cannot be checked from the markup;
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


# Two different ways a bit of a table can be too big for the space it is in,
# and only one of them was being checked.
#
#   scrollWidth > clientWidth   the element clips its OWN content. A pill is
#                               `white-space: nowrap` and has no overflow rule,
#                               so it never does this — it just gets wider.
#   right > cell.right          the element runs past the CELL it sits in. This
#                               is what actually renders as a chopped-off word,
#                               and it was invisible to the first check.
OVERFLOWING_BITS = """() => {
  const bad = [];
  for (const bit of document.querySelectorAll('.pill, .actions-cell a')) {
    const cell = bit.closest('td, th');
    if (bit.scrollWidth > bit.clientWidth + 1) bad.push(bit.textContent.trim() + ' (clips itself)');
    else if (cell && bit.getBoundingClientRect().right > cell.getBoundingClientRect().right + 1)
      bad.push(bit.textContent.trim() + ' (past its cell)');
  }
  return bad;
}"""


class TestTableFits:
    def test_the_drafts_table_does_not_need_sideways_scrolling(self, page, base_url):
        """Every column of Drafts is visible without scrolling the table.

        It scrolled on the office machines and not here, which is the tell: the
        invoice cell is `white-space: nowrap`, and a cell that cannot be made
        narrower than its text pushes a fixed-layout table past its declared
        width. Whether that overflows then depends on how wide the browser
        renders the monospace face — Firefox wider than Chromium, so the same
        page scrolled for them and not for me.

        Checked at several widths because one is not evidence.
        """
        for width in (1440, 1280, 1100):
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(f"{base_url}/drafts")
            page.wait_for_selector("table.list")
            over = page.evaluate("""() => {
              const t = document.querySelector('table.list');
              const wrap = document.querySelector('.table-wrap');
              return t.scrollWidth - wrap.clientWidth;
            }""")
            assert over <= 1, f"Drafts overflows by {over}px at {width}px wide"

            clipped = page.evaluate(OVERFLOWING_BITS)
            assert clipped == [], f"clipped at {width}px: {clipped}"

    def test_the_register_status_column_holds_both_badges(self, page, base_url):
        """A status cell now carries TWO badges — what the pass is, and whether
        it has reached paper — and the column has to be wide enough for the
        wider of them.

        This exists because checking `scrollWidth > clientWidth` on the badge
        itself said everything was fine while the second badge visibly ran past
        the edge of its column and rendered as "UNPRINTEI". A `nowrap` pill does
        not clip itself; it overflows its CELL. So the comparison that matters
        is against the containing cell, which is what OVERFLOWING_BITS does.
        """
        for width in (1920, 1440, 1280, 1100):
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(f"{base_url}/register")
            page.wait_for_selector("table.list")
            assert page.locator(".reg-status .pill").count() > 0, "no badges to check"
            clipped = page.evaluate(OVERFLOWING_BITS)
            assert clipped == [], f"badge past its cell at {width}px: {clipped}"


class TestRunningTotals:
    def test_totals_follow_the_rows_as_they_are_typed(self, page, base_url):
        """The figure updates on every keystroke, add and remove.

        Asserted in a browser because that is the only place it exists: the
        totals on the review screen are not posted and not stored, they are
        what the operator checks against the invoice in their hand before a
        number is spent.
        """
        TestItemTableKeyboard._open_a_draft(self, page, base_url)
        qty = page.locator("#total-qty-display")
        ctn = page.locator("#total-cartons-display")
        if qty.count() == 0:
            pytest.skip("no running totals on this screen")

        rows = page.locator("#items-table tbody tr")
        rows.nth(0).locator("input[name=quantity]").fill("4")
        rows.nth(0).locator("input[name=cartons]").fill("2")
        page.wait_for_timeout(100)
        assert qty.inner_text() == "4", "typing a quantity updates the total"
        assert ctn.inner_text() == "2", "typing a carton count updates the total"

        # A row added afterwards must count too — a listener bound per row is
        # one that gets forgotten on the next "+ Add item".
        page.click("#add-row")
        last = page.locator("#items-table tbody tr").last
        last.locator("input[name=item_name]").fill("EXTRA")
        last.locator("input[name=quantity]").fill("6")
        last.locator("input[name=cartons]").fill("3")
        page.wait_for_timeout(100)
        assert qty.inner_text() == "10", "a newly added row is counted"
        assert ctn.inner_text() == "5"

        last.locator(".remove-row").click()
        page.wait_for_timeout(100)
        assert qty.inner_text() == "4", "removing a row takes it back out"
        assert ctn.inner_text() == "2"

        # Blank and part-typed values are worth nothing, not NaN.
        rows.nth(0).locator("input[name=quantity]").fill("")
        page.wait_for_timeout(100)
        assert qty.inner_text() == "0", f"a blank quantity reads as 0, got {qty.inner_text()}"


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


CONTRAST = """(sel) => {
  const lum = (c) => {
    const [r, g, b] = c.match(/\\d+/g).map(Number).map((v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const el = document.querySelector(sel);
  if (!el) return null;
  const s = getComputedStyle(el);
  const a = lum(s.color), b = lum(s.backgroundColor);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}"""


class TestPrintButtonState:
    """The register shows what has reached paper on the Print button itself.

    Solid while the paper is still owed, quiet once it exists. Both have to stay
    legible, and both have to survive the theme flipping — which is the part
    that broke: the first version painted the solid one with --ink, which is
    "primary TEXT" and so goes near-white in dark mode. The button was
    white-on-white and completely invisible, and nothing in light mode showed
    it. That is why this test runs twice.
    """

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_both_states_are_legible_in_either_theme(self, page, base_url, scheme):
        page.emulate_media(color_scheme=scheme)
        page.goto(f"{base_url}/register")
        page.wait_for_selector("table.list")

        owed = page.evaluate(CONTRAST, "a.print-action.not-printed")
        if owed is None:
            pytest.skip("no unprinted pass in the register to look at")
        assert owed >= 4.5, f"the solid Print button is {owed:.2f}:1 in {scheme}"

        done = page.evaluate(CONTRAST, "a.print-action.is-printed")
        if done is not None:
            assert done >= 4.5, f"the quiet Print button is {done:.2f}:1 in {scheme}"
            # And they must not have converged into the same button.
            same = page.evaluate("""() => {
              const a = document.querySelector('a.print-action.not-printed');
              const b = document.querySelector('a.print-action.is-printed');
              return getComputedStyle(a).backgroundColor === getComputedStyle(b).backgroundColor;
            }""")
            assert not same, f"both states paint the same background in {scheme}"


class TestDeleteDraftFromTheRow:
    def test_the_cross_removes_the_draft_and_says_so(self, page, base_url):
        """The ✕ on a draft row: confirm, remove, toast, and the list settles.

        In a browser because none of it exists anywhere else — the confirm, the
        toast and the reload are the feature. The important part is the last
        one: the page reloads instead of unpicking the duplicate highlighting
        itself, so what the operator ends up looking at is the server's answer
        about what still blocks what.
        """
        # Two drafts from one file, so they share a document number and block.
        page.goto(f"{base_url}/upload")
        page.set_input_files("#invoice", [_fake_pdf("dupe-a.pdf"),
                                           _fake_pdf("dupe-b.pdf")])
        page.wait_for_selector("#file-panel:visible")
        page.click("#submit-btn")
        page.wait_for_url(re.compile(r"/(review|drafts)"), timeout=30000)
        page.goto(f"{base_url}/drafts")
        page.wait_for_selector("table.list")

        rows = page.locator("tbody tr")
        before = rows.count()
        if before < 2:
            pytest.skip("need at least two drafts to delete one")

        page.on("dialog", lambda d: d.accept())
        page.locator(".draft-delete").first.click()

        toast = page.locator(".toast")
        toast.wait_for(state="visible", timeout=5000)
        assert "Draft removed" in toast.inner_text()

        # It reloads, so wait for the settled list rather than the instant one.
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("table.list, .empty")
        after = page.locator("tbody tr").count()
        assert after == before - 1, f"expected {before - 1} rows, found {after}"

    def test_declining_the_confirmation_changes_nothing(self, page, base_url):
        """The ✕ sits next to Open, so saying no has to mean no."""
        page.goto(f"{base_url}/upload")
        page.set_input_files("#invoice", [_fake_pdf("keep-me.pdf")])
        page.wait_for_selector("#file-panel:visible")
        page.click("#submit-btn")
        page.wait_for_url(re.compile(r"/(review|drafts)"), timeout=30000)
        page.goto(f"{base_url}/drafts")
        page.wait_for_selector("table.list")

        before = page.locator("tbody tr").count()
        page.on("dialog", lambda d: d.dismiss())
        page.locator(".draft-delete").first.click()
        page.wait_for_timeout(600)

        assert page.locator(".toast").count() == 0, "toasted a delete that was declined"
        assert page.locator("tbody tr").count() == before, "removed a row anyway"
