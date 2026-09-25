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
            # The LAST link, not the first. The drafts list is in upload order
            # — oldest at the top — so the draft this helper just created is at
            # the bottom. Taking the first one opened somebody else's draft,
            # which had items in it already and made the totals assertions
            # fail for reasons that had nothing to do with totals.
            page.locator("a[href*='/review/']").last.click()
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
  // A transparent background is not black. getComputedStyle reports it as
  // rgba(0, 0, 0, 0), and reading only the digits scored every see-through
  // control against black -- a ghost button on a white row came out at
  // 4.41:1 when it reads at 4.76. Walk up to what actually shows through.
  const opaque = (node) => {
    for (let n = node; n; n = n.parentElement) {
      const bg = getComputedStyle(n).backgroundColor;
      const alpha = bg.startsWith("rgba") ? parseFloat(bg.split(",")[3]) : 1;
      if (alpha > 0) return bg;
    }
    return "rgb(255, 255, 255)";
  };
  const a = lum(getComputedStyle(el).color), b = lum(opaque(el));
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}"""


CONTRAST_AGAINST = """([selector, behind]) => {
  const lum = (c) => {
    const [r, g, b] = c.match(/[\\d.]+/g).slice(0, 3).map(Number).map((v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const fg = getComputedStyle(document.querySelector(selector)).color;
  const bg = getComputedStyle(document.querySelector(behind)).backgroundColor;
  const a = lum(fg), b = lum(bg);
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


class TestDropZoneIsReadable:
    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_the_drop_zone_can_be_read_in_either_theme(self, page, base_url, scheme):
        """The main control of the whole app, legible in both themes.

        Its panel was a hardcoded light colour while its heading was a token
        that goes near-white in dark mode: 1.05:1, invisible, and nothing in
        light mode showed it. Exactly the failure the Print button had, on a
        more important element.
        """
        page.emulate_media(color_scheme=scheme)
        page.goto(f"{base_url}/upload")
        page.wait_for_selector(".dropzone")
        for selector, floor in ((".dropzone-title", 4.5), (".dropzone-sub", 4.5)):
            got = page.evaluate(CONTRAST_AGAINST, [selector, ".dropzone"])
            assert got >= floor, f"{selector} is {got:.2f}:1 in {scheme}"


class TestManualGatePass:
    def test_cartons_come_from_the_master_list_times_the_quantity(self, page, base_url):
        """A fan listed as 2 cartons, three of them, is SIX cartons.

        Not three. The old behaviour copied the quantity into the carton box,
        which is right only for models that ship one-to-a-box and understates
        every FANDELIER and 100-inch GRANDMASTER — and understating boxes is
        how a box goes missing without anyone noticing.
        """
        page.goto(f"{base_url}/manual")
        page.wait_for_selector("#items-table")
        row = page.locator("#items-table tbody tr").first

        row.locator('input[name="item_name"]').fill("VENETIAN BLACK - FANDELIER")
        row.locator('input[name="quantity"]').fill("3")
        # The lookup is a debounced request, so wait for the answer.
        page.wait_for_function(
            """() => document.querySelector('#items-table tbody tr input[name=cartons]').value === '6'""",
            timeout=5000)

        # A one-carton model is the quantity, because that is what the list says.
        page.click("#add-row")
        second = page.locator("#items-table tbody tr").nth(1)
        second.locator('input[name="item_name"]').fill("AEROSLIM 1200MM WHITE")
        second.locator('input[name="quantity"]').fill("4")
        page.wait_for_function(
            """() => [...document.querySelectorAll('#items-table tbody tr')][1]
                     .querySelector('input[name=cartons]').value === '4'""",
            timeout=5000)

        assert page.locator("#total-qty-display").inner_text() == "7"
        assert page.locator("#total-cartons-display").inner_text() == "10"

    def test_an_unknown_item_is_left_blank_rather_than_guessed(self, page, base_url):
        """Not on the list means no number, and a line saying why.

        A blank asks the operator a question. A number invented from the
        quantity asserts something nobody worked out, on a document that gets
        signed at the gate.
        """
        page.goto(f"{base_url}/manual")
        page.wait_for_selector("#items-table")
        row = page.locator("#items-table tbody tr").first
        row.locator('input[name="item_name"]').fill("SOMETHING NOBODY HAS LISTED")
        row.locator('input[name="quantity"]').fill("5")
        page.wait_for_timeout(900)

        assert row.locator('input[name="cartons"]').input_value() == "", \
            "an unknown item was given a made-up carton count"
        assert "not on the master list" in row.locator(".carton-note").inner_text()

    def test_a_typed_carton_count_is_never_overwritten(self, page, base_url):
        """The override, which is the half that protects a correction."""
        page.goto(f"{base_url}/manual")
        page.wait_for_selector("#items-table")
        row = page.locator("#items-table tbody tr").first

        row.locator('input[name="item_name"]').fill("VENETIAN BLACK - FANDELIER")
        row.locator('input[name="quantity"]').fill("3")
        page.wait_for_function(
            """() => document.querySelector('#items-table tbody tr input[name=cartons]').value === '6'""",
            timeout=5000)

        row.locator('input[name="cartons"]').fill("1")
        row.locator('input[name="quantity"]').fill("4")
        page.wait_for_timeout(900)
        assert row.locator('input[name="cartons"]').input_value() == "1", \
            "a typed carton count was overwritten by a later lookup"

    def test_rows_can_be_added_and_removed(self, page, base_url):
        page.goto(f"{base_url}/manual")
        page.wait_for_selector("#items-table")
        rows = page.locator("#items-table tbody tr")

        page.click("#add-row")
        page.click("#add-row")
        assert rows.count() == 3
        rows.nth(2).locator(".remove-row").click()
        assert rows.count() == 2

        # The last row empties rather than vanishing: a pass with no rows is
        # not a pass, and the server refuses it anyway.
        rows.nth(1).locator(".remove-row").click()
        rows.nth(0).locator('input[name="item_name"]').fill("Ceiling Fan")
        rows.nth(0).locator(".remove-row").click()
        assert rows.count() == 1
        assert rows.nth(0).locator('input[name="item_name"]').input_value() == ""

    def test_the_serial_numbers_appear_in_order_on_the_printed_pass(self, page, base_url):
        """Typing a whole pass and issuing it lands on the printed page."""
        page.goto(f"{base_url}/manual")
        page.wait_for_selector("#items-table")
        page.fill("#supplier_name", "FANZART LLP")
        page.fill("#customer_name", "MBS DECOR LLP")
        page.fill("#invoice_no", "LP 262700338")
        row = page.locator("#items-table tbody tr").first
        row.locator('input[name="item_name"]').fill("Ceiling Fan")
        row.locator('input[name="quantity"]').fill("4")

        page.click("button[type=submit].primary")
        page.wait_for_url(re.compile(r"/print/\d+"), timeout=15000)
        body = page.locator("body").inner_text()
        assert "MBS DECOR LLP" in body
        assert "Ceiling Fan" in body


class TestOptimisticDraftDelete:
    def _two_drafts(self, page, base_url):
        page.goto(f"{base_url}/upload")
        page.set_input_files("#invoice", [_fake_pdf("opt-a.pdf"), _fake_pdf("opt-b.pdf")])
        page.wait_for_selector("#file-panel:visible")
        page.click("#submit-btn")
        page.wait_for_url(re.compile(r"/(review|drafts)"), timeout=30000)
        page.goto(f"{base_url}/drafts")
        page.wait_for_selector("table.list")

    def test_the_row_goes_immediately_without_waiting_for_the_server(self, page, base_url):
        """The row leaves on the click, not on the response.

        Asserted by holding the response back: the request is delayed by two
        seconds and the row still has to be gone well inside that. Without the
        delay this test would pass on a fast local server no matter how the
        code was written, which would make it worthless.
        """
        self._two_drafts(page, base_url)
        rows = page.locator("tbody tr")
        before = rows.count()
        if before < 2:
            pytest.skip("need at least two drafts")

        # The request is swallowed and NEVER answered, so anything that waits
        # on the response waits for ever. The row still has to go.
        in_flight = []
        page.route("**/delete", lambda route: in_flight.append(route))
        page.on("dialog", lambda d: d.accept())
        try:
            page.locator(".draft-delete").first.click()
            page.wait_for_timeout(500)

            visible = page.locator("tbody tr:not(.removing)").count()
            assert visible == before - 1, (
                f"the row was still shown with the request unanswered "
                f"({visible} of {before}) — the delete is waiting on the server")
            # And the count under the table moved with it, rather than still
            # claiming a draft that is no longer on screen.
            counts = page.locator("#ready-count-text").inner_text()
            assert f"of {before - 1} ready" in counts, (
                f"the count still reflects {before} drafts: {counts!r}")
        finally:
            for route in in_flight:
                route.abort()
            page.unroute_all(behavior="ignoreErrors")

    def test_a_failed_delete_puts_the_row_back(self, page, base_url):
        """A delete that fails must not look like one that worked."""
        self._two_drafts(page, base_url)
        rows = page.locator("tbody tr")
        before = rows.count()
        if before < 2:
            pytest.skip("need at least two drafts")

        first_id = page.locator(".draft-delete").first.get_attribute("data-draft-id")
        page.route("**/delete", lambda route: route.fulfill(
            status=500, content_type="application/json", body='{"ok": false}'))
        page.on("dialog", lambda d: d.accept())
        page.locator(".draft-delete").first.click()

        toast = page.locator(".toast.bad")
        toast.wait_for(state="visible", timeout=5000)
        assert "Failed to delete draft" in toast.inner_text()
        assert page.locator("tbody tr:not(.removing)").count() == before, \
            "the row was not restored after the delete failed"
        # And back in its own place, not appended at the bottom.
        assert page.locator(".draft-delete").first.get_attribute("data-draft-id") == first_id


class TestRegisterMasterCheckbox:
    """The header checkbox cycles instead of toggling.

    The reason anybody ticks boxes on this page is to print a batch, and the
    batch almost always wanted is "the ones not printed yet" — which otherwise
    means reading down the column picking them out by hand.
    """

    def _open(self, page, base_url):
        page.goto(f"{base_url}/register")
        page.wait_for_selector("table.list")
        master = page.locator("#select-all")
        if master.count() == 0:
            pytest.skip("this account cannot batch print, so there are no ticks")
        return master

    def _state(self, page):
        return page.evaluate("""() => {
          const ticks = [...document.querySelectorAll('.pass-tick')];
          const master = document.querySelector('#select-all');
          const unprinted = ticks.filter(t =>
            t.closest('tr').querySelector('a.print-action.not-printed'));
          return {
            total: ticks.length,
            unprinted: unprinted.length,
            checked: ticks.filter(t => t.checked).length,
            unprintedChecked: unprinted.filter(t => t.checked).length,
            masterChecked: master.checked,
            masterIndeterminate: master.indeterminate,
          };
        }""")

    def test_it_cycles_unprinted_then_all_then_none(self, page, base_url):
        master = self._open(page, base_url)
        before = self._state(page)
        if before["unprinted"] == 0 or before["unprinted"] == before["total"]:
            # The numbers, not just the reason: a skip that says only "need a
            # mix" is indistinguishable from a pass in the summary line, and
            # this test silently stopped running once before anybody noticed.
            pytest.skip("need a mix of printed and unprinted to see all three "
                        f"stages — page has {before['total']} passes, "
                        f"{before['unprinted']} of them unprinted")

        master.click()
        first = self._state(page)
        assert first["checked"] == first["unprinted"], (
            f"click 1 selected {first['checked']}, expected the "
            f"{first['unprinted']} unprinted")
        assert first["unprintedChecked"] == first["unprinted"]
        assert first["masterIndeterminate"], "click 1 should show a partial selection"
        assert not first["masterChecked"]

        master.click()
        second = self._state(page)
        assert second["checked"] == second["total"], "click 2 should select everything"
        assert second["masterChecked"] and not second["masterIndeterminate"]

        master.click()
        third = self._state(page)
        assert third["checked"] == 0, "click 3 should clear the selection"
        assert not third["masterChecked"] and not third["masterIndeterminate"]

        # And round again, so it is a cycle rather than a one-shot sequence.
        master.click()
        assert self._state(page)["checked"] == before["unprinted"]

    @pytest.mark.parametrize("uniform", ["all printed", "none printed"])
    def test_it_skips_the_unprinted_stage_when_it_would_be_a_dead_click(
            self, page, base_url, uniform):
        """With every pass in one print state, the first stage is dropped.

        Selecting "only the unprinted" when there are none selects nothing and
        looks broken; when they are ALL unprinted it is identical to selecting
        everything, so the SECOND click looks dead. Either way the cycle should
        collapse to all -> none, which is what a plain checkbox would do.

        The page is forced into each uniform state rather than skipped, because
        a branch that only runs on a register nobody has in front of them is a
        branch nobody has tested.
        """
        master = self._open(page, base_url)
        if uniform == "all printed":
            page.evaluate("""() => document.querySelectorAll('a.print-action.not-printed')
                               .forEach(a => a.classList.replace('not-printed', 'is-printed'))""")
        else:
            page.evaluate("""() => document.querySelectorAll('a.print-action.is-printed')
                               .forEach(a => a.classList.replace('is-printed', 'not-printed'))""")

        state = self._state(page)
        assert state["unprinted"] in (0, state["total"]), "the page is not uniform"

        master.click()
        after = self._state(page)
        assert after["checked"] == after["total"], (
            f"with a uniform page ({uniform}) the first click should select "
            f"everything, but selected {after['checked']} of {after['total']}")
        assert after["masterChecked"] and not after["masterIndeterminate"]

        master.click()
        cleared = self._state(page)
        assert cleared["checked"] == 0, "the second click should clear it"
        assert not cleared["masterChecked"] and not cleared["masterIndeterminate"]

    def test_ticking_a_row_by_hand_restarts_the_cycle(self, page, base_url):
        master = self._open(page, base_url)
        state = self._state(page)
        if state["unprinted"] == 0 or state["unprinted"] == state["total"]:
            pytest.skip("need a mix to tell the first stage from the second "
                        f"— page has {state['total']} passes, "
                        f"{state['unprinted']} of them unprinted")

        master.click()                       # stage 1: unprinted
        page.locator(".pass-tick").first.click()   # operator intervenes
        master.click()                       # should start again, not continue

        after = self._state(page)
        assert after["checked"] == after["unprinted"], (
            "after a manual tick the header should restart at 'unprinted', "
            f"but selected {after['checked']} of {after['total']}")


class TestUnsavedChangesGuard:
    """Leaving a half-typed pass asks first.

    Nothing on these screens is stored until the form is submitted, and a
    twenty-line item table is twenty minutes of somebody's afternoon. Asked for
    in review, after a draft edit was lost.

    Tested by dispatching beforeunload and reading defaultPrevented rather than
    by driving a real navigation: the browser's own dialog cannot be inspected,
    but whether the page asked for it can be, and that is the behaviour.
    """

    ASKED = """() => {
      const e = new Event('beforeunload', {cancelable: true});
      window.dispatchEvent(e);
      return e.defaultPrevented;
    }"""

    def test_an_untouched_form_leaves_without_a_word(self, page, base_url):
        page.goto(f"{base_url}/manual")
        page.wait_for_selector("#items-table")
        assert not page.evaluate(self.ASKED), \
            "an untouched form should never interrupt someone leaving"

    def test_a_typed_form_asks_before_leaving(self, page, base_url):
        page.goto(f"{base_url}/manual")
        page.wait_for_selector("#items-table")
        page.fill("#customer_name", "MBS DECOR LLP")
        assert page.evaluate(self.ASKED), \
            "a form with typing in it left without asking"

    def test_typing_only_in_the_item_table_still_counts(self, page, base_url):
        """The rows are inside the form, so editing one is editing the form."""
        page.goto(f"{base_url}/manual")
        page.wait_for_selector("#items-table")
        page.locator('#items-table tbody input[name="item_name"]').first.fill("Ceiling Fan")
        assert page.evaluate(self.ASKED)

    def test_submitting_is_not_leaving(self, page, base_url):
        """Issuing the pass must not be challenged by the guard."""
        page.goto(f"{base_url}/manual")
        page.wait_for_selector("#items-table")
        page.fill("#supplier_name", "FANZART LLP")
        page.fill("#customer_name", "MBS DECOR LLP")
        page.fill("#invoice_no", "LP 262700338")
        row = page.locator("#items-table tbody tr").first
        row.locator('input[name="item_name"]').fill("Ceiling Fan")
        row.locator('input[name="quantity"]').fill("4")

        page.on("dialog", lambda d: d.accept())
        page.click("button[type=submit].primary")
        page.wait_for_url(re.compile(r"/(print|manual)"), timeout=15000)
        # Whatever it landed on, it must not have been blocked by our guard.
        assert "/manual" not in page.url or page.locator(".notice").count() > 0, \
            "the guard interfered with a real submission"

    def test_the_edit_screen_guards_too(self, page, base_url):
        """The screen where losing work costs most: the pass is already printed."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("table.list")
        edit = page.locator("a[href*='/edit']")
        if edit.count() == 0:
            pytest.skip("this account cannot edit issued passes")
        edit.first.click()
        page.wait_for_selector("#items-table")

        assert not page.evaluate(self.ASKED), "untouched edit form asked to confirm"
        page.locator('#items-table tbody input[name="quantity"]').first.fill("99")
        assert page.evaluate(self.ASKED), "a changed correction left without asking"


class TestReviewDocumentPanel:
    """The uploaded PDF beside the fields it was read from.

    Asked for in review: an extraction error is obvious against the original
    and invisible without it — a wrong quantity looks exactly like a right one
    until you can see the invoice.
    """

    def _open_draft_with_pdf(self, page, base_url):
        page.goto(f"{base_url}/drafts")
        page.wait_for_selector("table.list")
        rows = page.locator("tbody tr")
        for i in range(rows.count()):
            row = rows.nth(i)
            link = row.locator("a[href*='/review/']")
            if link.count() == 0:
                continue
            link.first.click()
            page.wait_for_url(re.compile(r"/review/"))
            if page.locator("#review-document").count() > 0:
                return True
            page.goto(f"{base_url}/drafts")
            page.wait_for_selector("table.list")
        return False

    def test_the_original_is_shown_beside_the_form(self, page, base_url):
        if not self._open_draft_with_pdf(page, base_url):
            pytest.skip("no draft with its PDF still on disk")

        image = page.locator(".document-page")
        assert image.count() == 1, "the document panel is not on the page"
        assert page.locator("iframe").count() == 0, (
            "the document is embedded as a PDF again — every browser then draws "
            "it with its own viewer and they do not agree")
        src = image.get_attribute("src")
        assert "/page/" in src and src.endswith(".png"), \
            f"the panel is not showing a rendered page: {src}"
        # It actually loaded, rather than sitting there as broken alt text.
        assert page.evaluate(
            "() => document.querySelector('.document-page').naturalWidth > 0"), \
            "the rendered page did not load"

        # Side by side, not stacked, on a wide screen — the whole point.
        page.set_viewport_size({"width": 1600, "height": 1000})
        columns = page.evaluate(
            "() => getComputedStyle(document.getElementById('review-split')).gridTemplateColumns")
        assert len(columns.split()) == 2, f"expected two columns, got {columns!r}"

    def test_the_pdf_is_actually_served_and_framable(self, page, base_url):
        """X-Frame-Options is DENY app-wide, which would blank this panel.

        The header has to be relaxed to SAMEORIGIN for this one route or the
        frame renders empty — and an empty frame looks like a broken PDF
        rather than like a misconfigured header.
        """
        if not self._open_draft_with_pdf(page, base_url):
            pytest.skip("no draft with its PDF still on disk")

        src = page.locator(".document-page").get_attribute("src")
        response = page.request.get(f"{base_url}{src}")
        assert response.status == 200, f"the page image did not load: {response.status}"
        assert response.headers.get("content-type") == "image/png", (
            "the panel must be served a real image, got "
            f"{response.headers.get('content-type')!r}")
        # Customer documents on a shared machine, and the source PDF is deleted
        # once a pass is issued.
        assert "no-store" in response.headers.get("cache-control", ""), \
            "the rendered invoice must not be cached by the browser"
        # No size assertion. Other tests in this suite upload deliberately tiny
        # 16-byte stand-in PDFs, and whichever draft this one happens to open
        # may be one of them — "the file is big enough" is not something this
        # test can know. That it is served, and framable, is.
        assert response.body(), "the served file is empty"

    def test_it_looks_the_same_whatever_the_browser_would_have_done(self, page, base_url):
        """White page, full panel width, no viewer chrome.

        The point of rendering server-side: Chromium's PDF viewer paints a
        #323639 background with a thumbnail sidebar and a toolbar, and ignores
        the #toolbar=0&navpanes=0 parameters that used to turn those off.
        Firefox paints a clean light page. An image has none of that to
        disagree about.
        """
        if not self._open_draft_with_pdf(page, base_url):
            pytest.skip("no draft with its PDF still on disk")

        page.set_viewport_size({"width": 1500, "height": 1000})
        state = page.evaluate("""() => {
          const img = document.querySelector('.document-page');
          const vp = document.querySelector('.document-viewport');
          return {
            background: getComputedStyle(vp).backgroundColor,
            imageWidth: Math.round(img.getBoundingClientRect().width),
            panelWidth: vp.clientWidth,
            loaded: img.naturalWidth > 0,
          };
        }""")
        assert state["loaded"], "the page image did not load"
        # White, not the dark grey a Chromium PDF viewer would paint.
        assert state["background"] == "rgb(255, 255, 255)", \
            f"the document sits on {state['background']}, not white"
        # Fits the width of the panel, which is what somebody comparing a line
        # against a form actually wants.
        assert abs(state["imageWidth"] - state["panelWidth"]) <= 3, \
            f"image is {state['imageWidth']}px in a {state['panelWidth']}px panel"

    def test_hiding_it_is_remembered(self, page, base_url):
        if not self._open_draft_with_pdf(page, base_url):
            pytest.skip("no draft with its PDF still on disk")

        page.click("#hide-document")
        assert page.locator("#review-document").is_hidden()
        assert page.locator("#show-document").is_visible()
        columns = page.evaluate(
            "() => getComputedStyle(document.getElementById('review-split')).gridTemplateColumns")
        assert len(columns.split()) == 1, "the form should take the full width when hidden"

        # It is a preference of this browser, so it survives a reload.
        page.reload()
        page.wait_for_selector("#review-split")
        assert page.locator("#review-document").is_hidden(), \
            "hiding the document was not remembered"

        page.click("#show-document")
        page.reload()
        page.wait_for_selector("#review-split")
        assert page.locator("#review-document").is_visible()

    def test_it_stacks_rather_than_squeezing_on_a_narrow_screen(self, page, base_url):
        """Two half-width columns are worse than one of each."""
        if not self._open_draft_with_pdf(page, base_url):
            pytest.skip("no draft with its PDF still on disk")

        page.set_viewport_size({"width": 900, "height": 950})
        columns = page.evaluate(
            "() => getComputedStyle(document.getElementById('review-split')).gridTemplateColumns")
        assert len(columns.split()) == 1, f"should stack at 900px, got {columns!r}"
        assert not page.evaluate(
            "() => document.documentElement.scrollWidth > window.innerWidth + 1"), \
            "the page scrolls sideways at 900px"


class TestStickerQueueValidation:
    """Nothing incomplete, and nothing twice, gets into the print queue.

    A sticker is printed onto pre-printed stationery that the office buys by
    the sheet. A job queued with no LR number prints a blank where the
    consignment number belongs, and the sheet is spoilt; two jobs sharing an
    LR number print two sets of labels for one consignment, and the wrong set
    goes on a box. Both are caught before the queue, not at the printer.
    """

    def _open(self, page, base_url):
        page.goto(f"{base_url}/stickers")
        page.wait_for_selector("#generate")
        page.evaluate("() => localStorage.removeItem('sm_sticker_print_queue')")
        page.reload()
        page.wait_for_selector("#generate")

    def _fill(self, page, lr, receiver, qty):
        page.fill("#lr", lr)
        page.fill("#receiver", receiver)
        page.fill("#qty", qty)

    def _state(self, page):
        return page.evaluate("""() => ({
          hidden: document.getElementById('sticker-problem').hidden,
          text: document.getElementById('sticker-problem').textContent.trim(),
          marked: [...document.querySelectorAll('.sticker-form .is-missing')].map((e) => e.id),
          rows: document.querySelectorAll('.sticker-queue-table tbody tr').length,
        })""")

    def test_every_empty_field_is_named_at_once(self, page, base_url):
        """Four blank boxes should be four complaints, not four round trips."""
        self._open(page, base_url)
        self._fill(page, "", "", "")
        page.click("#generate")

        state = self._state(page)
        assert not state["hidden"], "an empty form was queued without a word"
        assert state["rows"] == 0, "an empty job reached the queue"
        assert state["marked"] == ["lr", "receiver", "qty"], \
            f"wrong boxes marked: {state['marked']}"
        for field in ("LR Number", "Receiver", "Number of Boxes"):
            assert field in state["text"], f"{field!r} missing from {state['text']!r}"

    @pytest.mark.parametrize("qty", ["0", "-3"])
    def test_a_job_of_no_boxes_is_refused(self, page, base_url, qty):
        """Clicked, not dispatched -- the button is what the office uses.

        min="1" on the input made Chrome swallow the submit event outright,
        so the handler never ran and a 0 produced a native bubble in one
        browser and nothing in another. The form is novalidate now, which
        this test is here to keep true: driving it by hand would pass even
        with the attribute back.
        """
        self._open(page, base_url)
        self._fill(page, "LR-1", "CHENNAI", qty)
        page.click("#generate")

        state = self._state(page)
        assert not state["hidden"], f"a job of {qty} boxes was queued silently"
        assert state["rows"] == 0, f"a job of {qty} boxes reached the queue"
        assert state["marked"] == ["qty"], f"wrong boxes marked: {state['marked']}"
        assert "Number of Boxes" in state["text"]

    def test_typing_takes_the_red_off_that_box_alone(self, page, base_url):
        """The warning goes when it stops being true, not on the first key.

        Filling one of three empty boxes must take the red off that box and
        leave the message up, because two boxes are still empty. Clearing the
        lot on the first keystroke would send somebody to the printer with
        two blanks still on the form.
        """
        self._open(page, base_url)
        self._fill(page, "", "", "")
        page.click("#generate")
        assert len(self._state(page)["marked"]) == 3

        page.fill("#lr", "LR-9")
        state = self._state(page)
        assert state["marked"] == ["receiver", "qty"], \
            f"filling one box changed the wrong marks: {state['marked']}"
        assert not state["hidden"], "the message went while two boxes were still empty"

        self._fill(page, "LR-9", "CHENNAI", "4")
        state = self._state(page)
        assert state["marked"] == [], f"still marked: {state['marked']}"
        assert state["hidden"], "the message stayed after everything was filled"

    def test_choosing_a_receiver_from_the_list_clears_its_red(self, page, base_url):
        """Setting .value in code fires no event, so this needs its own clearing."""
        self._open(page, base_url)
        self._fill(page, "LR-1", "", "2")
        page.click("#generate")
        assert "receiver" in self._state(page)["marked"]

        page.click("#receiver")
        page.wait_for_selector(".receiver-menu li")
        page.click(".receiver-menu li")
        assert "receiver" not in self._state(page)["marked"], \
            "the box stayed red after being filled from the list"

    def test_the_same_lr_number_cannot_be_queued_twice(self, page, base_url):
        """Case and stray spaces do not make it a different consignment."""
        self._open(page, base_url)
        self._fill(page, "LR-1", "CHENNAI", "4")
        page.click("#generate")
        assert self._state(page)["rows"] == 1

        self._fill(page, "  lr-1  ", "MYSURU", "2")
        page.click("#generate")

        state = self._state(page)
        assert state["rows"] == 1, "the duplicate was queued anyway"
        assert not state["hidden"], "the duplicate was dropped without a word"
        assert state["marked"] == ["lr"], f"wrong boxes marked: {state['marked']}"
        assert "row 1" in state["text"], \
            f"the clashing row is not named: {state['text']!r}"

    def test_editing_a_row_does_not_clash_with_itself(self, page, base_url):
        """Correcting the box count must not be refused for the LR it already has."""
        self._open(page, base_url)
        self._fill(page, "LR-1", "CHENNAI", "4")
        page.click("#generate")
        self._fill(page, "LR-2", "MYSURU", "2")
        page.click("#generate")
        assert self._state(page)["rows"] == 2

        page.click(".sticker-queue-table tbody tr:nth-child(1) .queue-edit")
        page.fill("#qty", "9")
        page.click("#generate")

        state = self._state(page)
        assert state["hidden"], f"editing a row clashed with itself: {state['text']!r}"
        assert state["rows"] == 2, "editing changed the number of rows"
        assert page.locator(
            ".sticker-queue-table tbody tr:nth-child(1) .q-qty").inner_text().strip() == "9", \
            "the corrected box count was not saved"

    def test_editing_a_row_onto_another_rows_lr_is_refused(self, page, base_url):
        self._open(page, base_url)
        self._fill(page, "LR-1", "CHENNAI", "4")
        page.click("#generate")
        self._fill(page, "LR-2", "MYSURU", "2")
        page.click("#generate")

        page.click(".sticker-queue-table tbody tr:nth-child(1) .queue-edit")
        page.fill("#lr", "LR-2")
        page.click("#generate")

        state = self._state(page)
        assert not state["hidden"], "two rows were allowed to share an LR number"
        assert state["marked"] == ["lr"], f"wrong boxes marked: {state['marked']}"

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_the_complaint_is_legible_in_either_theme(self, page, base_url, scheme):
        """A warning nobody can read is not a warning.

        The register once shipped a label at 3.96:1 that had to be found by
        somebody squinting at it. The danger colour is one of the few that
        gets used on a tinted panel rather than the card, so it is worth
        pinning in both themes.
        """
        page.emulate_media(color_scheme=scheme)
        self._open(page, base_url)
        self._fill(page, "", "", "")
        page.click("#generate")

        got = page.evaluate(CONTRAST_AGAINST, [".sticker-problem", ".sticker-problem"])
        assert got >= 4.5, f"the warning is {got:.2f}:1 in {scheme}"

    def test_cancelling_an_edit_takes_the_complaint_with_it(self, page, base_url):
        """The next person to use the form should not inherit the last one's red."""
        self._open(page, base_url)
        self._fill(page, "LR-1", "CHENNAI", "4")
        page.click("#generate")

        page.click(".sticker-queue-table tbody tr:nth-child(1) .queue-edit")
        page.fill("#lr", "")
        page.click("#generate")
        assert not self._state(page)["hidden"]

        page.click("#queue-cancel-edit")
        state = self._state(page)
        assert state["hidden"], f"the complaint outlived the edit: {state['text']!r}"
        assert state["marked"] == [], f"still marked: {state['marked']}"


class TestReceiverList:
    """The pick list and the manage panel both hang off the receiver field.

    Both float over the page. The manage panel used to sit in the flow, so
    opening it pushed Add, Print and the whole queue a few hundred pixels
    down -- on a page whose value is that the buttons are where you left
    them, twenty times a morning.
    """

    def _open(self, page, base_url):
        page.goto(f"{base_url}/stickers")
        page.wait_for_selector("#generate")

    def test_opening_the_manage_panel_moves_nothing(self, page, base_url):
        self._open(page, base_url)
        before = page.locator("#generate").bounding_box()

        page.click("#receiver-manage")
        page.wait_for_selector("#receiver-list:not([hidden])")
        after = page.locator("#generate").bounding_box()

        assert abs(after["y"] - before["y"]) < 1, (
            f"Add moved {after['y'] - before['y']:.0f}px when the panel opened")

    def test_the_panel_floats_over_the_form_rather_than_stretching_it(
            self, page, base_url):
        """If it is in the flow it makes the card taller; a popover does not."""
        self._open(page, base_url)
        height = "() => document.querySelector('.sticker-form').getBoundingClientRect().height"
        before = page.evaluate(height)

        page.click("#receiver-manage")
        page.wait_for_selector("#receiver-list:not([hidden])")
        assert abs(page.evaluate(height) - before) < 1, "the form card grew"

        # And it really is on top of what it covers, not behind it.
        assert page.evaluate("""() => {
          const p = document.getElementById('receiver-list').getBoundingClientRect();
          return document.elementFromPoint(p.x + p.width / 2, p.y + p.height - 8)
                 .closest('#receiver-list') !== null;
        }"""), "something is drawn over the manage panel"

    def test_the_name_fits_the_box_it_is_typed_into(self, page, base_url):
        """The caret belongs inside the field.

        Beside it, the caret and the Manage button took 68px out of a 254px
        column and the longest real receiver showed as "CHENNAI (SUM...".
        """
        self._open(page, base_url)
        page.fill("#receiver", "CHENNAI (SUMANGALI)")
        overflow = page.evaluate(
            "() => { const b = document.getElementById('receiver');"
            "        return b.scrollWidth - b.clientWidth; }")
        assert overflow <= 0, f"the receiver name is clipped by {overflow}px"

    def test_only_one_of_the_two_is_ever_open(self, page, base_url):
        """They sit in the same place, so the second swallows the first's clicks."""
        self._open(page, base_url)
        page.click("#receiver-manage")
        page.wait_for_selector("#receiver-list:not([hidden])")

        page.click("#receiver")
        page.wait_for_selector(".receiver-menu li")
        assert page.locator("#receiver-list").is_hidden(), \
            "the manage panel stayed open under the pick list"

        page.click("#receiver-manage")
        page.wait_for_selector("#receiver-list:not([hidden])")
        assert page.locator("#receiver-menu").is_hidden(), \
            "the pick list stayed open under the manage panel"

    def test_the_close_button_shuts_the_panel(self, page, base_url):
        self._open(page, base_url)
        page.click("#receiver-manage")
        page.wait_for_selector("#receiver-list:not([hidden])")

        page.click("#receiver-close")
        assert page.locator("#receiver-list").is_hidden(), "the panel stayed open"

    def test_a_name_can_still_be_added_and_removed(self, page, base_url):
        """The looks changed; the list did not."""
        self._open(page, base_url)
        page.click("#receiver-manage")
        page.wait_for_selector("#receiver-list:not([hidden])")

        page.fill("#receiver-new", "COIMBATORE")
        page.click("#receiver-add")
        names = page.locator("#receiver-items li span").all_inner_texts()
        assert "COIMBATORE" in names, f"not added: {names}"

        row = page.locator("#receiver-items li",
                           has=page.locator("span", has_text="COIMBATORE"))
        row.locator(".receiver-remove").click()
        names = page.locator("#receiver-items li span").all_inner_texts()
        assert "COIMBATORE" not in names, f"not removed: {names}"

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_both_panels_read_as_lifted_off_the_page(self, page, base_url, scheme):
        """A popover with no shadow is a rectangle of text over more text.

        The pick list carried a hardcoded 12%-black shadow, which on the dark
        theme's near-black card was not visible at all.
        """
        page.emulate_media(color_scheme=scheme)
        self._open(page, base_url)

        page.click("#receiver-manage")
        page.wait_for_selector("#receiver-list:not([hidden])")
        for selector in ("#receiver-list", "#receiver-menu"):
            if selector == "#receiver-menu":
                page.click("#receiver")
                page.wait_for_selector(".receiver-menu li")
            shadow = page.evaluate(
                f"() => getComputedStyle(document.querySelector('{selector}')).boxShadow")
            assert shadow and shadow != "none", f"{selector} has no shadow in {scheme}"


class TestStickerTypeface:
    """The three values print in regular Arial, at normal weight -- not bold.

    Not Arial Black either: Arial Black is heavier than bold by design, so
    it cannot be un-bolded, and "not bold, whatever the face" is the rule.
    Plain "Arial" is also the family name every Windows browser resolves
    reliably ("Arial Black" was not). Liberation Sans is its metric twin on
    Linux, so a sheet laid out here fits exactly as it will on Windows.
    """

    def _card(self, page, base_url):
        page.goto(f"{base_url}/stickers")
        page.wait_for_selector("#generate")
        page.evaluate("() => localStorage.removeItem('sm_sticker_print_queue')")
        page.reload()
        page.wait_for_selector("#generate")
        page.fill("#lr", "71703457")
        page.fill("#receiver", "CHENNAI")
        page.fill("#qty", "3")
        page.click("#generate")
        page.wait_for_selector(".sticker-card")

    @pytest.mark.parametrize(
        "selector,pt", [(".sticker-lr", 29), (".sticker-from", 29), (".sticker-to", 25)])
    def test_each_line_is_regular_arial_at_normal_weight(
            self, page, base_url, selector, pt):
        self._card(page, base_url)
        page.emulate_media(media="print")
        got = page.evaluate(
            f"""() => {{
              const cs = getComputedStyle(document.querySelector('{selector}'));
              return {{family: cs.fontFamily, weight: cs.fontWeight, size: cs.fontSize,
                       synthesis: cs.fontSynthesisWeight === 'none' ? 'none'
                                  : (cs.fontSynthesis || 'unset')}};
            }}""")

        assert got["family"].lower().startswith("arial,"), \
            f"{selector} asks for {got['family']!r} first"
        assert got["weight"] == "400", \
            f"{selector} is weight {got['weight']} -- it must not be bold"
        # Nothing in this stack may be faked: a synthesised weight is how a
        # missing font disguises itself as a present one.
        assert got["synthesis"] in ("none", "weight style small-caps"), got["synthesis"]
        # pt -> px at the CSS 96dpi reference, which is what the document's
        # measurements were taken in.
        expected = pt * 96 / 72
        assert abs(float(got["size"].rstrip("px")) - expected) < 0.1, \
            f"{selector} is {got['size']}, expected {expected:.2f}px ({pt}pt)"

    @pytest.mark.parametrize(
        "selector", [".awb-text", ".origin-text", ".dest-text"])
    def test_the_on_screen_preview_uses_it_too(self, page, base_url, selector):
        """Screen media, deliberately.

        Every other test here emulates print, so all of them kept passing
        with the screen rule deleted. The preview is what somebody checks
        before committing a sheet of stationery to the printer; if it shows a
        different typeface, it is not a preview.
        """
        self._card(page, base_url)
        got = page.evaluate(
            f"""() => {{
              const cs = getComputedStyle(document.querySelector('{selector}'));
              return {{family: cs.fontFamily, weight: cs.fontWeight,
                       synthesis: cs.fontSynthesisWeight}};
            }}""")
        assert got["family"].lower().startswith("arial,"), \
            f"{selector} asks for {got['family']!r} first on screen"
        # The browser may never fake a bold weight here, whatever face it
        # lands on -- the print rules say the same, but screen is its own
        # cascade and the preview is what people check before printing.
        assert got["synthesis"] == "none", \
            f"{selector} lets the browser synthesise weight: {got['synthesis']!r}"
        assert got["weight"] == "400", f"{selector} is weight {got['weight']} on screen"

    @pytest.mark.parametrize(
        "selector", [".awb-text", ".origin-text", ".dest-text"])
    def test_the_print_cascade_carries_the_same_typeface(
            self, page, base_url, selector):
        """Print is a separate cascade in every engine.

        Screen being right proves nothing about paper, and paper is the one
        that costs pre-printed stationery when it is wrong.
        """
        self._card(page, base_url)
        page.emulate_media(media="print")
        got = page.evaluate(
            f"""() => {{
              const cs = getComputedStyle(document.querySelector('{selector}'));
              return {{family: cs.fontFamily, weight: cs.fontWeight}};
            }}""")
        assert got["family"].lower().startswith("arial,"), \
            f"{selector} asks for {got['family']!r} first on paper"
        assert got["weight"] == "400", \
            f"{selector} prints at weight {got['weight']}"

    def test_nothing_heavier_than_regular_is_in_the_stack(self, page, base_url):
        """No "Arial Black", no "Arial Bold": either would print bold."""
        self._card(page, base_url)
        family = page.evaluate(
            "() => getComputedStyle(document.querySelector('.dest-text')).fontFamily").lower()
        for heavy in ("black", "bold", "impact"):
            assert heavy not in family, f"{heavy!r} is in the sticker stack: {family}"

    def test_the_printed_sheet_carries_a_regular_face_only(self, page, base_url, tmp_path):
        """What reaches paper, read out of the PDF itself.

        Computed styles say what was asked for; the PDF says what the engine
        actually used. A bold face here would be named ...-Bold.
        """
        self._card(page, base_url)
        page.emulate_media(media="print")
        pdf = tmp_path / "sheet.pdf"
        page.pdf(path=str(pdf), prefer_css_page_size=True)
        import re
        fonts = set(re.findall(rb"/BaseFont\s*/(?:[A-Z]{6}\+)?([A-Za-z0-9-]+)", pdf.read_bytes()))
        assert fonts, "no fonts found in the printed PDF"
        heavy = [f for f in fonts if re.search(rb"bold|black|heavy", f, re.I)]
        assert not heavy, f"the printed sheet uses a heavy face: {sorted(fonts)}"

    def test_a_long_destination_keeps_its_full_size(self, page, base_url):
        """Regular Arial is narrow enough that the longest real receiver fits
        its 110mm blank at the full 25pt -- nothing is squeezed."""
        page.goto(f"{base_url}/stickers")
        page.wait_for_selector("#generate")
        page.evaluate("() => localStorage.removeItem('sm_sticker_print_queue')")
        page.reload()
        page.wait_for_selector("#generate")
        page.fill("#lr", "552655525")
        page.fill("#receiver", "CHENNAI (SUMANGALI)")
        page.fill("#qty", "1")
        page.click("#generate")
        page.wait_for_timeout(300)
        fit = page.evaluate("""() => { const e = document.querySelector('.dest-text');
          return {over: e.scrollWidth - e.clientWidth,
                  pt: parseFloat(getComputedStyle(e).fontSize) * 0.75}; }""")
        assert fit["over"] <= 0, f"the destination overflows by {fit['over']}px"
        assert abs(fit["pt"] - 25) < 0.01, f"the destination was shrunk to {fit['pt']}pt"

    def test_the_ghost_labels_beside_the_values_stay_bold(self, page, base_url):
        """Only the DATA changed.

        The grey "AWB No: / ORIGIN: / DESTINATION:" drawn on screen stands in
        for what the courier has already printed on the sheet. It is a guide,
        not a value, and it never reaches paper -- so it keeps its own weight.
        """
        self._card(page, base_url)
        weight = page.evaluate(
            "() => getComputedStyle(document.querySelector('.sticker-card'), '::before')"
            "        .fontWeight")
        assert weight == "700", f"the ghost labels went to {weight}"


class TestQueueClearsAfterPrinting:
    """A printed batch does not survive into the next one.

    The labels are gone the moment they leave the printer, so a queue that
    stays behind is a queue somebody prints twice -- a second set of stickers
    for boxes that already have them, which is the same failure the duplicate
    LR check exists to stop.
    """

    def _queued(self, page, base_url, jobs=(("LR-1", "CHENNAI", "4"),
                                            ("LR-2", "MYSURU", "2"))):
        page.goto(f"{base_url}/stickers")
        page.wait_for_selector("#generate")
        page.evaluate("() => localStorage.removeItem('sm_sticker_print_queue')")
        page.reload()
        page.wait_for_selector("#generate")
        # A real window.print() opens a dialog nothing can dismiss here. The
        # stub fires the same event the browser fires when the dialog closes,
        # so what is under test is our handler, not Chromium's dialog.
        page.evaluate(
            "() => { window.print = () => window.dispatchEvent(new Event('afterprint')); }")
        for lr, receiver, qty in jobs:
            page.fill("#lr", lr)
            page.fill("#receiver", receiver)
            page.fill("#qty", qty)
            page.click("#generate")

    def _rows(self, page):
        return page.locator(".sticker-queue-table tbody tr").count()

    def test_printing_empties_the_queue_and_the_stored_copy(self, page, base_url):
        self._queued(page, base_url)
        assert self._rows(page) == 2

        page.click("#print-stickers")
        page.wait_for_timeout(200)

        assert self._rows(page) == 0, "the queue survived the print"
        assert page.locator(".sticker-card").count() == 0, "the sheet still has cards"
        assert page.evaluate(
            "() => window.localStorage.getItem('sm_sticker_print_queue')") is None, \
            "the stored queue was left behind"

    def test_the_empty_state_comes_back(self, page, base_url):
        self._queued(page, base_url)
        page.click("#print-stickers")
        page.wait_for_timeout(200)
        assert page.locator("text=No stickers yet").is_visible(), \
            "the sheet did not return to its empty state"

    def test_it_stays_empty_after_a_reload(self, page, base_url):
        """Clearing the array is not enough if the browser still holds a copy."""
        self._queued(page, base_url)
        page.click("#print-stickers")
        page.wait_for_timeout(200)

        page.reload()
        page.wait_for_selector("#generate")
        assert self._rows(page) == 0, "the printed queue came back on reload"

    def test_the_alignment_test_page_leaves_the_queue_alone(self, page, base_url):
        """It also calls window.print(), and it is not a print of the batch.

        Wiping somebody's queue because they checked the printer would be
        the opposite of helpful.
        """
        self._queued(page, base_url)
        if not page.locator("#align-test").count():
            pytest.skip("no alignment test button for this user")

        page.click("#align-test")
        page.wait_for_timeout(200)
        assert self._rows(page) == 2, "the alignment test print wiped the queue"

    def test_an_empty_queue_printing_nothing_is_harmless(self, page, base_url):
        """Nothing queued, nothing to clear, no error."""
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base_url}/stickers")
        page.wait_for_selector("#generate")
        page.evaluate("() => { window.print = () =>"
                      " window.dispatchEvent(new Event('afterprint')); window.print(); }")
        page.wait_for_timeout(100)
        assert not errors, f"afterprint on an empty queue threw: {errors}"


class TestQueueActionStyling:
    """Clear and delete are the two controls that destroy work."""

    def _queued(self, page, base_url):
        page.goto(f"{base_url}/stickers")
        page.wait_for_selector("#generate")
        page.evaluate("() => localStorage.removeItem('sm_sticker_print_queue')")
        page.reload()
        page.wait_for_selector("#generate")
        page.fill("#lr", "LR-1")
        page.fill("#receiver", "CHENNAI")
        page.fill("#qty", "4")
        page.click("#generate")

    def test_clear_is_a_red_ghost_that_turns_solid_under_the_pointer(
            self, page, base_url):
        """Secondary and destructive: tinted at rest, solid red on intent."""
        self._queued(page, base_url)
        button = page.locator("#queue-clear")
        assert button.inner_text().strip() == "Clear All", \
            f"the button says {button.inner_text().strip()!r}"

        read = """() => {
          const cs = getComputedStyle(document.getElementById('queue-clear'));
          return {bg: cs.backgroundColor, color: cs.color};
        }"""
        page.mouse.move(0, 0)
        page.wait_for_timeout(400)     # past the transition; see SETTLE below
        rest = page.evaluate(read)
        assert rest["bg"] == "rgb(254, 242, 242)", f"at rest Clear is {rest['bg']}"
        assert rest["color"] == "rgb(239, 68, 68)", f"at rest its label is {rest['color']}"

        button.hover()
        page.wait_for_timeout(400)
        hover = page.evaluate(read)
        assert hover["bg"] == "rgb(220, 38, 38)", f"under the pointer Clear is {hover['bg']}"
        assert hover["color"] == "rgb(255, 255, 255)", hover

    def test_the_delete_cross_is_red_before_it_is_hovered(self, page, base_url):
        """Hover-only red hides which control throws the row away."""
        self._queued(page, base_url)
        colour = page.evaluate(
            "() => getComputedStyle(document.querySelector('.queue-remove')).color")
        assert colour == "rgb(220, 53, 69)", f"the cross is {colour}"

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_the_clear_button_is_legible_in_either_theme(self, page, base_url, scheme):
        """It carries its own colours, so it does not follow the theme tokens.

        Measured against what is actually behind the label: the dark-theme
        ghost is a 12% red tint over the card, so its background has to be
        composited, not read as if it were opaque red.

        The light-theme floor is 3.4, not 4.5, on purpose. #ef4444 on #fef2f2
        is the colour pair that was asked for, and it measures 3.44:1 -- under
        the AA line for 13px text. #b91c1c would clear it comfortably. This
        pins the known value so it cannot quietly get worse.
        """
        page.emulate_media(color_scheme=scheme)
        self._queued(page, base_url)
        page.mouse.move(0, 0)
        page.wait_for_timeout(400)
        got = page.evaluate(r"""() => {
          const rgba = (c) => { const v = c.match(/[\d.]+/g).map(Number);
                                return [v[0], v[1], v[2], v.length > 3 ? v[3] : 1]; };
          const lum = ([r, g, b]) => [r, g, b].map((v) => { v /= 255;
              return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); })
            .reduce((a, v, i) => a + v * [0.2126, 0.7152, 0.0722][i], 0);
          const btn = getComputedStyle(document.getElementById('queue-clear'));
          const [r, g, b, a] = rgba(btn.backgroundColor);
          const card = rgba(getComputedStyle(document.querySelector('.sticker-queue')).backgroundColor);
          const bg = [0, 1, 2].map((i) => [r, g, b][i] * a + card[i] * (1 - a));
          const x = lum(rgba(btn.color)), y = lum(bg);
          return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
        }""")
        floor = 3.4 if scheme == "light" else 4.5
        assert got >= floor, f"Clear is {got:.2f}:1 in {scheme}"


class TestDeployedChangesReachTheBrowser:
    """A deploy that the office cannot see has not happened.

    The stylesheet is requested with the file's modification time on the end,
    so a new stylesheet is a new URL. That only works if the browser re-reads
    the PAGE and sees the new stamp. Flask sends no cache headers of its own,
    so a browser was free to hold the page, keep quoting the old stamp, and
    answer it from the stylesheet it had cached for a week -- showing the old
    design while the server was provably serving the new one.
    """

    def test_pages_are_never_cached(self, page, base_url):
        response = page.goto(f"{base_url}/stickers")
        cache = (response.header_value("cache-control") or "").lower()
        assert "no-store" in cache, f"the page may be cached: {cache!r}"

    def test_the_stylesheet_is_asked_for_by_version(self, page, base_url):
        """The stamp is what makes a changed file a different URL."""
        page.goto(f"{base_url}/stickers")
        href = page.evaluate(
            "() => document.querySelector('link[rel=stylesheet]').getAttribute('href')")
        assert "v=" in href, f"no cache-busting stamp on the stylesheet: {href!r}"

    def test_the_stamp_changes_when_the_file_does(self, page, base_url, tmp_path):
        """A stamp that never moves is decoration, not cache-busting."""
        import os
        import re

        page.goto(f"{base_url}/stickers")
        first = page.evaluate(
            "() => document.querySelector('link[rel=stylesheet]').getAttribute('href')")
        stamp = re.search(r"v=(\d+)", first).group(1)

        css = Path(__file__).resolve().parents[2] / "static" / "css" / "style.css"
        original = css.stat().st_mtime
        try:
            os.utime(css, (original + 60, original + 60))
            page.goto(f"{base_url}/stickers")
            second = page.evaluate(
                "() => document.querySelector('link[rel=stylesheet]').getAttribute('href')")
            assert re.search(r"v=(\d+)", second).group(1) != stamp, \
                "the stylesheet URL did not change when the file did"
        finally:
            os.utime(css, (original, original))


class TestActionButtonStates:
    """Add and Print are used standing at a printer, with one hand.

    They are sized for that, and Print says what it will do before it is
    read: grey when there is nothing queued, green when there is.
    """

    # These buttons carry `transition: all .2s`, and getComputedStyle during a
    # transition returns the value mid-animation. Reading a colour straight
    # after the click that changes it returns the OLD one -- which looks
    # exactly like the rule not applying. Every colour read here waits first.
    SETTLE = 400

    def _open(self, page, base_url):
        page.goto(f"{base_url}/stickers")
        page.wait_for_selector("#generate")
        page.evaluate("() => localStorage.removeItem('sm_sticker_print_queue')")
        page.reload()
        page.wait_for_selector("#generate")
        # Off the buttons, or whichever one the pointer lands on reports its
        # hover colour instead of its resting one.
        page.mouse.move(0, 0)
        page.wait_for_timeout(self.SETTLE)

    def _style(self, page, selector):
        page.wait_for_timeout(self.SETTLE)
        return page.evaluate(
            f"""() => {{
              const e = document.querySelector('{selector}');
              const cs = getComputedStyle(e);
              return {{bg: cs.backgroundColor, color: cs.color, weight: cs.fontWeight,
                       size: cs.fontSize, cursor: cs.cursor, display: cs.display,
                       disabled: e.disabled, active: e.classList.contains('btn-print-active')}};
            }}""")

    def _queue_one(self, page):
        page.fill("#lr", "71703457")
        page.fill("#receiver", "CHENNAI")
        page.fill("#qty", "4")
        page.click("#generate")
        page.mouse.move(0, 0)

    def test_add_is_sized_for_a_warehouse_not_a_form(self, page, base_url):
        self._open(page, base_url)
        style = self._style(page, "#generate")
        assert style["size"] == "15px", f"Add is {style['size']}"
        height = page.locator("#generate").bounding_box()["height"]
        assert height >= 40, f"Add is only {height:.0f}px tall"

        # Not an assertion about `display`: these are flex items, and CSS
        # blockifies a flex item's inline-flex to flex, so the computed value
        # is "flex" however it was written. What matters is that the row
        # lines up, so measure that.
        self._queue_one(page)
        add = page.locator("#generate").bounding_box()
        count = page.locator("#sticker-count").bounding_box()
        assert abs((add["y"] + add["height"] / 2)
                   - (count["y"] + count["height"] / 2)) < 2, \
            "the count does not sit on Add's centre line"

    def test_the_top_row_is_only_for_building_the_batch(self, page, base_url):
        """Print moved to the queue card; nothing that finishes a batch is here."""
        self._open(page, base_url)
        self._queue_one(page)
        in_top_row = page.evaluate(
            "() => [...document.querySelectorAll('.sticker-actions button')]"
            "        .filter((b) => !b.hidden).map((b) => b.id)")
        assert in_top_row == ["generate"], f"top row holds {in_top_row}"

    def test_clear_and_print_are_a_matched_pair_on_the_queue(self, page, base_url):
        """Same box, same line, side by side, in the card they act on."""
        self._open(page, base_url)
        self._queue_one(page)
        page.wait_for_timeout(self.SETTLE)

        both = page.evaluate("""() => ['#queue-clear', '#print-stickers'].map((s) => {
          const e = document.querySelector(s); const cs = getComputedStyle(e);
          const r = e.getBoundingClientRect();
          return {inHeader: !!e.closest('.sticker-queue-head .queued-header-actions'),
                  w: Math.round(r.width), h: Math.round(r.height), top: Math.round(r.top),
                  left: r.left, pad: cs.padding, size: cs.fontSize, radius: cs.borderRadius};
        })""")
        clear, prnt = both
        assert clear["inHeader"] and prnt["inHeader"], \
            "Clear and Print are not both in the Queued header"
        for key in ("w", "h", "top", "pad", "size", "radius"):
            assert clear[key] == prnt[key], \
                f"the pair differs on {key}: Clear {clear[key]} vs Print {prnt[key]}"
        assert clear["size"] == "13px" and clear["pad"] == "8px 16px", clear
        assert clear["w"] >= 80, f"the buttons are only {clear['w']}px wide"
        assert clear["left"] < prnt["left"], "Print should sit to the right of Clear"

    def test_add_is_the_solid_dark_one(self, page, base_url):
        self._open(page, base_url)
        style = self._style(page, "#generate")
        assert style["bg"] == "rgb(15, 23, 42)", f"Add is {style['bg']}"
        assert style["color"] == "rgb(255, 255, 255)", f"Add's label is {style['color']}"
        assert style["weight"] == "600", f"Add is weight {style['weight']}"

    def test_print_is_grey_and_unclickable_with_nothing_queued(self, page, base_url):
        self._open(page, base_url)
        style = self._style(page, "#print-stickers")
        assert style["disabled"] is True, "Print is clickable with an empty queue"
        assert style["active"] is False, "Print claims to be ready with nothing queued"
        assert style["bg"] == "rgb(226, 232, 240)", f"Print is {style['bg']}"
        assert style["cursor"] == "not-allowed", f"cursor is {style['cursor']}"

    def test_print_is_a_green_ghost_that_fills_under_the_pointer(self, page, base_url):
        """Clear's pattern in Print's colour: tinted at rest, solid on intent."""
        self._open(page, base_url)
        self._queue_one(page)

        style = self._style(page, "#print-stickers")
        assert style["disabled"] is False, "Print is still disabled with a job queued"
        assert style["active"] is True, "the active class was not applied"
        assert style["bg"] == "rgb(240, 253, 244)", f"at rest Print is {style['bg']}"
        assert style["color"] == "rgb(22, 163, 74)", f"at rest its label is {style['color']}"
        assert style["weight"] == "600", f"Print is weight {style['weight']}"

        before = page.locator("#print-stickers").bounding_box()["y"]
        page.hover("#print-stickers")
        hover = self._style(page, "#print-stickers")
        assert hover["bg"] == "rgb(22, 163, 74)", f"under the pointer Print is {hover['bg']}"
        assert hover["color"] == "rgb(255, 255, 255)", hover
        after = page.locator("#print-stickers").bounding_box()["y"]
        assert after < before, "Print does not lift under the pointer"

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_the_print_label_is_legible_on_its_tint(self, page, base_url, scheme):
        """Measured against what is behind the label, tint composited over card.

        Replaces the old dark-card check: a ghost's background is meant to
        be close to the card, so what can vanish now is the label, not the
        box.

        The light floor is 3.1, not 4.5, on purpose. #16a34a on #f0fdf4 is the
        pair that was chosen and it measures 3.15:1 -- well under AA for 13px
        text, and fainter than Clear beside it. #15803d on the same tint is
        4.79:1. This pins the known value so it cannot quietly get worse.
        """
        page.emulate_media(color_scheme=scheme)
        self._open(page, base_url)
        self._queue_one(page)
        page.wait_for_timeout(self.SETTLE)
        got = page.evaluate(r"""() => {
          const rgba = (c) => { const v = c.match(/[\d.]+/g).map(Number);
                                return [v[0], v[1], v[2], v.length > 3 ? v[3] : 1]; };
          const lum = ([r, g, b]) => [r, g, b].map((v) => { v /= 255;
              return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); })
            .reduce((a, v, i) => a + v * [0.2126, 0.7152, 0.0722][i], 0);
          const btn = getComputedStyle(document.getElementById('print-stickers'));
          const [r, g, b, a] = rgba(btn.backgroundColor);
          const card = rgba(getComputedStyle(document.querySelector('.sticker-queue')).backgroundColor);
          const bg = [0, 1, 2].map((i) => [r, g, b][i] * a + card[i] * (1 - a));
          const x = lum(rgba(btn.color)), y = lum(bg);
          return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
        }""")
        floor = 3.1 if scheme == "light" else 4.5
        assert got >= floor, f"Print's label is {got:.2f}:1 in {scheme}"

    def test_it_goes_back_to_grey_when_the_queue_empties(self, page, base_url):
        """The attribute and the class are one fact, so they move together."""
        self._open(page, base_url)
        self._queue_one(page)
        assert self._style(page, "#print-stickers")["active"] is True

        page.once("dialog", lambda d: d.accept())
        page.click("#queue-clear")
        page.mouse.move(0, 0)

        style = self._style(page, "#print-stickers")
        assert style["active"] is False, "Print stayed green with an empty queue"
        assert style["disabled"] is True, "Print stayed clickable with an empty queue"

    def test_neither_button_reaches_the_paper(self, page, base_url):
        """They sit inside .no-print; this is what says so on purpose."""
        self._open(page, base_url)
        self._queue_one(page)
        page.emulate_media(media="print")
        # .no-print hides the CONTAINER. A descendant of a display:none
        # element keeps its own computed display -- reading the button's own
        # `display` says "flex" and proves nothing. What matters is that it
        # is inside a hidden container and has no box on the page.
        for selector in ("#generate", "#print-stickers", "#queue-clear"):
            result = page.evaluate(
                f"""() => {{
                  const el = document.querySelector('{selector}');
                  const hider = el.closest('.no-print');
                  return {{hidden: !!hider
                             && getComputedStyle(hider).display === 'none',
                           boxes: el.getClientRects().length}};
                }}""")
            assert result["hidden"], f"{selector} is not inside a hidden container"
            assert result["boxes"] == 0, f"{selector} still has a box on paper"

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_add_still_looks_like_a_button_in_either_theme(
            self, page, base_url, scheme):
        """#0f172a is all but the dark theme's own card colour.

        On dark it measured 1.05:1 against the card: no edges, just the white
        word floating there. The label stayed perfectly readable, which is
        why it is easy to miss -- it does not look broken, it looks like text.
        """
        page.emulate_media(color_scheme=scheme)
        self._open(page, base_url)
        # CONTRAST_AGAINST compares a foreground COLOUR to a background. Here
        # both sides are backgrounds -- the button's against the card's --
        # because the question is whether the button has an edge, not whether
        # its label is readable. The label was always readable; that is what
        # made this easy to miss.
        got = page.evaluate(r"""() => {
          const lum = (c) => {
            const [r, g, b] = c.match(/[\d.]+/g).slice(0, 3).map(Number).map((v) => {
              v /= 255;
              return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
            });
            return 0.2126 * r + 0.7152 * g + 0.0722 * b;
          };
          const a = lum(getComputedStyle(document.getElementById('generate')).backgroundColor);
          const b = lum(getComputedStyle(document.querySelector('.sticker-form')).backgroundColor);
          return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
        }""")
        assert got >= 1.5, \
            f"Add is {got:.2f}:1 against the card in {scheme} -- it has no visible edge"


class TestStickerPageMockup:
    """The layout from the design mockups: nav icons, required marks, the
    Queued Stickers header, icon actions -- and the details that make them
    work rather than merely appear."""

    def _open(self, page, base_url, width=1280):
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(f"{base_url}/stickers")
        page.wait_for_selector("#generate")
        page.evaluate("() => localStorage.removeItem('sm_sticker_print_queue')")
        page.reload()
        page.wait_for_selector("#generate")

    def _queue(self, page, jobs=(("74563", "CHENNAI (SUMANGALI)", "4"),
                                 ("82345678", "HYDERABAD", "12"))):
        for lr, receiver, qty in jobs:
            page.fill("#lr", lr)
            page.fill("#receiver", receiver)
            page.fill("#qty", qty)
            page.click("#generate")

    def test_every_nav_link_carries_an_icon_where_there_is_room(self, page, base_url):
        self._open(page, base_url, width=1280)
        links = page.evaluate("""() => [...document.querySelectorAll('.topbar nav > a')].map((a) => ({
            text: a.querySelector('span') && a.querySelector('span').textContent.trim(),
            icon: !!a.querySelector('svg.icon'),
            shown: a.querySelector('svg.icon')
                   && getComputedStyle(a.querySelector('svg.icon')).display !== 'none'}))""")
        assert links, "no nav links"
        missing = [l["text"] for l in links if not (l["icon"] and l["shown"])]
        assert not missing, f"nav links without a visible icon: {missing}"

    def test_the_nav_never_pushes_the_page_sideways(self, page, base_url):
        """The icons cost ~160px; below 1100px they yield rather than overflow."""
        for width in (1280, 1100, 1024, 900):
            self._open(page, base_url, width=width)
            over = page.evaluate(
                "() => document.documentElement.scrollWidth - window.innerWidth")
            assert over <= 1, f"the page scrolls sideways by {over}px at {width}px"

    def test_the_four_required_fields_are_marked(self, page, base_url):
        self._open(page, base_url)
        for field in ("lr", "sender", "receiver", "qty"):
            got = page.evaluate(f"""() => {{
              const mark = document.querySelector('label[for="{field}"] .req');
              return {{mark: mark && mark.textContent,
                       colour: mark && getComputedStyle(mark).color,
                       required: document.getElementById('{field}').getAttribute('aria-required')}};
            }}""")
            assert got["mark"] == "*", f"{field} has no asterisk"
            assert got["colour"] == "rgb(239, 68, 68)", f"{field}'s asterisk is {got['colour']}"
            # The star is decoration; this is what a screen reader hears.
            assert got["required"] == "true", f"{field} is not aria-required"

    def test_the_three_fields_have_no_placeholder(self, page, base_url):
        self._open(page, base_url)
        for field in ("lr", "receiver", "qty"):
            assert page.get_attribute(f"#{field}", "placeholder") is None, \
                f"#{field} still has a placeholder"

    def test_add_keeps_its_plus_through_an_edit(self, page, base_url):
        """Edit mode renames the button. It must rename the WORD only."""
        self._open(page, base_url)
        self._queue(page)
        add = page.locator("#generate")
        assert add.locator("svg.icon-plus").count() == 1
        assert add.inner_text().strip() == "Add"

        page.click(".sticker-queue-table tbody tr:nth-child(1) .queue-edit")
        assert add.inner_text().strip() == "Save Changes"
        assert add.locator("svg.icon-plus").count() == 1, "editing wiped the plus icon"

        page.click("#queue-cancel-edit")
        assert add.inner_text().strip() == "Add"
        assert add.locator("svg.icon-plus").count() == 1, "cancelling wiped the plus icon"

    def test_the_header_names_the_batch_and_its_size(self, page, base_url):
        self._open(page, base_url)
        self._queue(page)
        assert page.inner_text(".sticker-queue-head h2").strip() == "Queued Stickers"
        assert page.locator(".queued-title svg.icon-list").count() == 1
        # 4 + 12 boxes = 16 stickers, three to a sheet = 6 sheets.
        assert page.inner_text("#queue-badge").strip() == "16 stickers, 6 sheets"

        style = page.evaluate("""() => { const cs = getComputedStyle(document.getElementById('queue-badge'));
          return {bg: cs.backgroundColor, color: cs.color, radius: cs.borderRadius}; }""")
        assert style["bg"] == "rgb(239, 246, 255)" and style["color"] == "rgb(37, 99, 235)", style

    def test_the_badge_takes_no_space_when_nothing_is_queued(self, page, base_url):
        self._open(page, base_url)
        self._queue(page, jobs=(("1", "X", "1"),))
        page.once("dialog", lambda d: d.accept())
        page.click("#queue-clear")
        assert page.evaluate(
            "() => getComputedStyle(document.getElementById('queue-badge')).display") == "none"

    def test_clear_all_and_print_all_carry_their_icons(self, page, base_url):
        self._open(page, base_url)
        self._queue(page)
        for selector, word, icon in (("#queue-clear", "Clear All", "icon-trash-2"),
                                     ("#print-stickers", "Print All", "icon-printer")):
            button = page.locator(selector)
            assert button.inner_text().strip() == word, button.inner_text()
            assert button.locator(f"svg.{icon}").count() == 1, f"{word} has no {icon}"

    def test_rows_have_a_pencil_and_a_red_trash_under_actions(self, page, base_url):
        self._open(page, base_url)
        self._queue(page)
        assert page.inner_text(".sticker-queue-table thead th.q-act").strip().upper() == "ACTIONS"
        row = page.locator(".sticker-queue-table tbody tr").first
        assert row.locator(".queue-edit svg.icon-pencil").count() == 1
        trash = row.locator(".queue-remove")
        assert trash.locator("svg.icon-trash-2").count() == 1
        assert trash.inner_text().strip() == "", "the old x character is still there"
        assert page.evaluate(
            "() => getComputedStyle(document.querySelector('.queue-remove')).color") \
            == "rgb(220, 53, 69)", "the trash is not red"


class TestUploadTruckProgress:
    """A drawn truck, in a round pin, rides the front of the upload progress bar.

    Each invoice's server response is held here and released one at a time,
    so the bar can be inspected at every step -- locally a batch finishes
    faster than anything could look at it.
    """

    def _start(self, page, base_url, count=3):
        files = [_fake_pdf(f"truck{i}.pdf") for i in range(count)]
        held = []
        page.route("**/upload/one", lambda route: held.append(route))
        page.goto(f"{base_url}/upload")
        page.wait_for_selector("#invoice", state="attached")
        page.set_input_files("#invoice", files)
        page.click("#submit-btn")
        for _ in range(100):
            if len(held) == count:
                break
            page.wait_for_timeout(50)
        assert len(held) == count, f"only {len(held)} of {count} uploads started"
        return held

    def _answer(self, route, n):
        route.fulfill(status=200, content_type="application/json",
                      body='{"ok": true, "draft_id": %d, "problem": ""}' % (1000 + n))

    def _state(self, page):
        page.wait_for_timeout(500)      # past the 0.3s slide
        return page.evaluate("""() => {
          const track = document.getElementById('progress-track');
          const fill = document.getElementById('progress-bar').getBoundingClientRect();
          const pin = document.querySelector('.truck-indicator-wrapper');
          const t = pin.getBoundingClientRect();
          return {edge: fill.right, truck: t.left + t.width / 2,
                  driving: track.classList.contains('is-driving'),
                  anim: getComputedStyle(pin.querySelector('.truck-svg')).animationName,
                  valuenow: track.getAttribute('aria-valuenow'),
                  hidden: pin.getAttribute('aria-hidden'),
                  svg: !!pin.querySelector('svg.truck-svg'),
                  text: pin.textContent.trim()};
        }""")

    def test_the_truck_rides_the_front_of_the_fill_all_the_way(self, page, base_url):
        held = self._start(page, base_url)
        for step, expected in enumerate(("0", "33", "67")):
            s = self._state(page)
            assert abs(s["truck"] - s["edge"]) <= 2, \
                f"at {expected}% the truck is {s['truck'] - s['edge']:.0f}px off the fill's edge"
            assert s["valuenow"] == expected, s
            assert s["driving"] and s["anim"] == "driving-bounce", \
                f"the truck is not bouncing while reading: {s}"
            self._answer(held[step], step)

    def test_it_parks_when_the_batch_is_done(self, page, base_url):
        """Bouncing at 100% would read as "not done yet"."""
        seen = []
        page.expose_function("recordDriving", lambda v: seen.append(v))
        held = self._start(page, base_url)
        page.evaluate("""() => new MutationObserver(() => window.recordDriving(
            document.getElementById('progress-track').classList.contains('is-driving')))
            .observe(document.getElementById('progress-track'),
                     {attributes: true, attributeFilter: ['class']})""")
        for i, route in enumerate(held):
            self._answer(route, i)
        # The page moves on to the next screen once the batch is done, so the
        # recorded class changes are the evidence, not the page itself.
        for _ in range(100):
            if False in seen:
                break
            page.wait_for_timeout(50)
        assert seen and seen[-1] is False, f"the truck never parked: {seen}"

    def test_the_truck_is_drawn_not_an_emoji(self, page, base_url):
        """An emoji is a different picture in a different colour on every PC."""
        self._start(page, base_url)
        s = self._state(page)
        assert s["svg"], "the pin holds no drawn truck"
        assert s["text"] == "", f"there is still text in the pin: {s['text']!r}"
        colour = page.evaluate(
            "() => getComputedStyle(document.querySelector('.truck-indicator-wrapper')).color")
        assert colour == "rgb(37, 99, 235)", f"the truck is {colour}, not the brand blue"

    def test_the_truck_is_decoration_to_a_screen_reader(self, page, base_url):
        self._start(page, base_url)
        s = self._state(page)
        assert s["hidden"] == "true"
        assert page.get_attribute("#progress-track", "role") == "progressbar"

    def test_no_bounce_for_anyone_who_asked_for_less_motion(self, page, base_url):
        """It still moves along the bar -- that is information -- but does not bounce."""
        page.emulate_media(reduced_motion="reduce")
        self._start(page, base_url)
        assert self._state(page)["anim"] == "none"


class TestLrNumberDigitsOnly:
    """The LR Number box takes digits and nothing else.

    Typed with real key presses, because the filter runs on the input event
    and a test that set the value directly would bypass it.
    """

    def _open(self, page, base_url, query=""):
        page.goto(f"{base_url}/stickers{query}")
        page.wait_for_selector("#generate")
        page.evaluate("() => localStorage.removeItem('sm_sticker_print_queue')")
        if not query:
            page.reload()
            page.wait_for_selector("#generate")

    @pytest.mark.parametrize("typed,kept", [
        ("lhkj", ""), ("12ab34", "1234"), ("7170-3457", "71703457"), ("00123", "00123"),
    ])
    def test_only_digits_survive_typing(self, page, base_url, typed, kept):
        """Leading zeros included: an LR is an identifier, not a quantity."""
        self._open(page, base_url)
        page.locator("#lr").press_sequentially(typed)
        assert page.input_value("#lr") == kept

    def test_a_letter_mid_number_leaves_the_cursor_where_it_was(self, page, base_url):
        self._open(page, base_url)
        lr = page.locator("#lr")
        lr.press_sequentially("1234")
        lr.press("ArrowLeft")
        lr.press("ArrowLeft")
        lr.press_sequentially("x")
        assert lr.input_value() == "1234"
        assert page.evaluate("() => document.getElementById('lr').selectionStart") == 2, \
            "the cursor jumped when the letter was dropped"
        lr.press_sequentially("5")
        assert lr.input_value() == "12534", "typing on after a dropped letter went astray"

    def test_a_paste_keeps_just_the_digits(self, page, base_url):
        self._open(page, base_url)
        page.focus("#lr")
        page.evaluate("""() => { const e = document.getElementById('lr');
          e.setRangeText('LR-7170 3457', 0, e.value.length, 'end');
          e.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertFromPaste'})); }""")
        assert page.input_value("#lr") == "71703457"

    def test_an_lr_with_letters_from_a_link_is_refused_not_queued(self, page, base_url):
        """Nothing was typed, so nothing was filtered -- Add is the backstop."""
        self._open(page, base_url, "?lr=LR5566&receiver=MUMBAI&qty=2")
        page.wait_for_timeout(300)
        assert page.locator(".sticker-queue-table tbody tr").count() == 0, \
            "a non-numeric LR from a link was queued"
        page.click("#generate")
        assert page.is_visible("#sticker-problem")
        assert "digits only" in page.inner_text("#sticker-problem")
        assert page.evaluate(
            "() => document.getElementById('lr').classList.contains('is-missing')"), \
            "the LR box is not marked"
        assert page.locator(".sticker-queue-table tbody tr").count() == 0
