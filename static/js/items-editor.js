/* The item table that appears on three screens: review, manual and edit.
 *
 * It was written three times. The copies had already drifted — the edit screen
 * called its delete button `row-remove` while the other two and the stylesheet
 * called it `remove-row`, so that button had no styling at all and nobody
 * noticed, because the page it was on looked fine to whoever had last worked
 * on one of the other two.
 *
 * That is the argument for one copy. The behaviours below are not decoration;
 * each of them exists because of something that happened:
 *
 *   Enter walks ACROSS the row and falls into the next one, because that is
 *   the order a line is read off an invoice. It used to move down, which meant
 *   typing a row needed the mouse between every cell.
 *
 *   Enter NEVER submits, on any of the three screens. The submit button on all
 *   of them spends or rewrites a gate pass number, and a stray keystroke must
 *   not reach it.
 *
 *   The arrows move down a column, for the other way of working: filling in
 *   every carton count in one pass once the names are typed.
 *
 *   The last row empties rather than disappearing. A pass with no items is not
 *   a pass, the server refuses it, and a table with no rows leaves nothing to
 *   type into.
 *
 * Everything is delegated from the tbody, so rows added later need nothing
 * bound to them — a listener attached per row is one that gets forgotten by
 * the next "+ Add item".
 */
(function (global) {
  "use strict";

  function sumColumn(tbody, name) {
    // Blanks and part-typed values count as nothing rather than turning the
    // sum into NaN: the carton column is routinely left empty to be written in
    // by hand, and a quantity someone is midway through typing is not an error.
    let total = 0;
    tbody.querySelectorAll(`input[name="${name}"]`).forEach((el) => {
      const n = parseInt(el.value.trim(), 10);
      if (!Number.isNaN(n)) total += n;
    });
    return total;
  }

  function attach(options) {
    const table = document.querySelector(options.table || "#items-table");
    if (!table) return null;
    // Per-call, not per-module: two editors on one page would otherwise share
    // one object and the second would overwrite the first's methods.
    const api = {};
    const tbody = table.querySelector("tbody");
    const form = table.closest("form");
    const itemsPerPage = options.itemsPerPage || 0;
    const pageNote = options.pageNote ? document.querySelector(options.pageNote) : null;
    const rowCount = options.rowCount ? document.querySelector(options.rowCount) : null;
    const qtyOut = document.querySelector(options.totalQty || "#total-qty-display");
    const ctnOut = document.querySelector(options.totalCartons || "#total-cartons-display");

    function recalcTotals() {
      if (qtyOut) qtyOut.textContent = sumColumn(tbody, "quantity");
      if (ctnOut) ctnOut.textContent = sumColumn(tbody, "cartons");
    }

    function renumber() {
      const rows = [...tbody.querySelectorAll("tr")];
      rows.forEach((row, i) => {
        const cell = row.querySelector(".sl-no") || row.cells[0];
        if (cell) cell.textContent = i + 1;
      });

      if (rowCount && itemsPerPage) {
        const pages = Math.max(1, Math.ceil(rows.length / itemsPerPage));
        rowCount.textContent = pages > 1
          ? `${rows.length} items — ${pages} pages`
          : `${rows.length} of ${itemsPerPage} rows used`;
        rowCount.classList.remove("over-limit");
        // A long invoice is not a problem: it stays ONE gate pass with one
        // number and simply prints across more sheets. Saying so stops people
        // splitting an order by hand to make it fit.
        if (pageNote) {
          if (pages > 1) {
            pageNote.textContent =
              `This gate pass contains ${rows.length} items. It will print across ` +
              `${pages} pages under one number.`;
            pageNote.hidden = false;
          } else {
            pageNote.hidden = true;
          }
        }
      }
      recalcTotals();
    }

    function blankRowHtml() {
      return `
        <td class="sl-no"></td>
        <td><input type="text" name="item_name" placeholder="Item name" autocomplete="off">
            <div class="hint carton-note"></div></td>
        <td class="col-qty-cell"><input type="text" name="quantity" inputmode="numeric"></td>
        <td class="col-ctn-cell"><input type="text" name="cartons" inputmode="numeric"></td>
        <td class="col-act-cell"><button type="button" class="remove-row"
            title="Remove row" aria-label="Remove row">&times;</button></td>`;
    }

    const addButton = options.addButton
      ? document.querySelector(options.addButton) : null;
    if (addButton) {
      addButton.addEventListener("click", () => {
        const row = document.createElement("tr");
        row.innerHTML = blankRowHtml();
        tbody.appendChild(row);
        renumber();
        row.querySelector('input[name="item_name"]').focus();
      });
    }

    tbody.addEventListener("click", (e) => {
      if (!e.target.classList.contains("remove-row")) return;
      if (tbody.rows.length === 1) {
        tbody.rows[0].querySelectorAll("input").forEach((el) => {
          el.value = "";
          delete el.dataset.touched;
        });
        const note = tbody.rows[0].querySelector(".carton-note");
        if (note) note.textContent = "";
      } else {
        e.target.closest("tr").remove();
      }
      renumber();
    });

    tbody.addEventListener("keydown", (e) => {
      const cell = e.target;
      if (cell.tagName !== "INPUT") return;
      const isEnter = e.key === "Enter";
      const vertical = e.key === "ArrowDown" ? 1 : e.key === "ArrowUp" ? -1 : 0;
      if (!isEnter && !vertical) return;
      e.preventDefault();

      const rows = [...tbody.querySelectorAll("tr")];
      const row = cell.closest("tr");
      let next = null;
      if (isEnter) {
        // Every input in document order, so "the next one" crosses the row
        // boundary by itself and no wrapping arithmetic is needed.
        const all = [...tbody.querySelectorAll("input")];
        next = all[all.indexOf(cell) + 1] || null;
      } else {
        const target = rows[rows.indexOf(row) + vertical];
        const column = [...row.children].indexOf(cell.closest("td"));
        next = target && target.children[column]
             ? target.children[column].querySelector("input") : null;
      }
      if (next) { next.focus(); next.select(); }
    });

    // ------------------------------------------------ cartons from the master
    // Only where the screen asks for it. The number is worked out on the
    // SERVER, by the same function the PDF upload path uses — multiplying here
    // would put a second copy of that rule in the browser for the two to
    // disagree over. This asks; it does not calculate.
    let lookupTimer = null;
    function refreshCartons(row) {
      if (!options.cartonLookupUrl) return;
      const item = row.querySelector('input[name="item_name"]');
      const qty = row.querySelector('input[name="quantity"]');
      const cartons = row.querySelector('input[name="cartons"]');
      if (!item || !qty || !cartons || cartons.dataset.touched === "1") return;

      const name = item.value.trim();
      const note = row.querySelector(".carton-note");
      if (!name) {
        cartons.value = "";
        if (note) note.textContent = "";
        recalcTotals();
        return;
      }
      const params = new URLSearchParams({q: name, quantity: qty.value.trim()});
      fetch(`${options.cartonLookupUrl}?${params}`)
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (!data) return;
          cartons.value = data.cartons === null ? "" : data.cartons;
          // Say WHY a box is blank, rather than leaving it looking broken.
          if (note) {
            note.textContent = data.non_stock
              ? "spare or service line — no carton count"
              : data.per_unit === null ? "not on the master list"
              : `${data.per_unit} carton(s) per unit`;
          }
          recalcTotals();
        })
        .catch(() => { /* offline: leave whatever is in the box alone */ });
    }

    function suggest(input) {
      if (!options.cartonLookupUrl) return;
      const term = input.value.trim();
      if (term.length < 2) return;
      let list = input.list;
      if (!list) {
        list = document.createElement("datalist");
        list.id = `fans-${Math.random().toString(36).slice(2)}`;
        document.body.appendChild(list);
        input.setAttribute("list", list.id);
      }
      fetch(`${options.cartonLookupUrl}?q=${encodeURIComponent(term)}`)
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (!data) return;
          list.innerHTML = "";
          (data.suggestions || []).forEach((name) => {
            const option = document.createElement("option");
            option.value = name;
            list.appendChild(option);
          });
        })
        .catch(() => { /* suggestions are a convenience, never a blocker */ });
    }

    tbody.addEventListener("input", (e) => {
      const cell = e.target;
      const row = cell.closest("tr");
      // Typing in the carton box takes ownership of it. That is the whole
      // difference between a default and an override: without it, correcting a
      // row to "4 fans, 1 carton" and then fixing a typo in the quantity would
      // silently throw the correction away.
      if (cell.name === "cartons") {
        cell.dataset.touched = "1";
      } else if (options.cartonLookupUrl
                 && (cell.name === "item_name" || cell.name === "quantity")) {
        clearTimeout(lookupTimer);
        lookupTimer = setTimeout(() => refreshCartons(row), 250);
        if (cell.name === "item_name") suggest(cell);
      }
      if (cell.name === "quantity" || cell.name === "cartons") recalcTotals();
    });

    // Picking from the datalist fires change rather than input in some browsers.
    tbody.addEventListener("change", (e) => {
      if (e.target.name === "item_name") refreshCartons(e.target.closest("tr"));
    });

    // A carton count that arrived already filled in was put there by a person
    // — either typed before a failed submission, or read off the invoice — so
    // it must not start following the quantity now.
    tbody.querySelectorAll('input[name="cartons"]').forEach((el) => {
      if (el.value.trim()) el.dataset.touched = "1";
    });

    // ------------------------------------------------------- unsaved changes
    // Nothing on these screens is stored until the form is submitted, and a
    // twenty-line item table is twenty minutes of somebody's afternoon. The
    // browser's own dialog is used rather than a custom one: it is the only
    // thing that can actually stop a navigation, and people recognise it.
    if (options.guardUnsaved && form) {
      let dirty = false;
      let submitting = false;
      form.addEventListener("input", () => { dirty = true; });
      form.addEventListener("change", () => { dirty = true; });
      // Submitting is not leaving. Neither is a button that navigates on
      // purpose, like Discard — those set this themselves via releaseGuard().
      form.addEventListener("submit", () => { submitting = true; });
      global.addEventListener("beforeunload", (e) => {
        if (!dirty || submitting) return;
        e.preventDefault();
        // Browsers ignore custom text now and show their own wording; the
        // assignment is what actually triggers the prompt.
        e.returnValue = "";
      });
      api.releaseGuard = () => { submitting = true; };
      api.isDirty = () => dirty && !submitting;
    }

    renumber();
    api.renumber = renumber;
    api.recalcTotals = recalcTotals;
    return api;
  }

  global.ItemsEditor = {attach: attach, sumColumn: sumColumn};
})(window);
