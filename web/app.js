// Shared helpers for the congress-trades static site. Pure data display - no return/alpha
// calculations, no benchmark comparisons, no "is this a good trade" framing. Every page
// fetches pre-built JSON from data/ (produced by scripts/export_website_data.py) - never a
// live database query, so visitor traffic never touches the backend at all.

const TYPE_LABELS = {
  purchase: "Buy",
  sale_full: "Sell (Full)",
  sale_partial: "Sell (Partial)",
  exchange: "Exchange",
};

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

// Every page's top-level data load otherwise has no .catch at all: fetchJSON throws on a
// non-OK response (e.g. a bad deploy leaving a stale/missing JSON file), and an uncaught
// rejection just leaves the page's "Loading…" text sitting there forever with nothing
// visible to the visitor - only a console error. Call this from a page's own catch block to
// turn that into a real, visible message in place of the loading indicator.
function showLoadError(el, detail) {
  if (!el) return;
  el.textContent = "Couldn't load data" + (detail ? ` (${detail})` : "") + " — try refreshing the page.";
  el.classList.add("error");
  el.style.display = "block";
}

async function fetchJSON(path) {
  // no-store, not just a reload: the export reruns once a day and a visitor's browser
  // otherwise has no signal that e.g. data/trades/564.json changed since their last visit
  // (the server sends no cache-control headers) - confirmed this causes real staleness
  // during development, where a page reload still served a fetch() response cached from
  // several edits ago at the exact same URL.
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`Failed to load ${path}: ${res.status}`);
  return res.json();
}

function formatAmount(low, high) {
  const fmt = (n) => "$" + Number(n).toLocaleString("en-US");
  if (high == null || high === low) return fmt(low) + "+";
  return `${fmt(low)} - ${fmt(high)}`;
}

function formatDate(dateStr) {
  if (!dateStr) return "—";
  const d = new Date(dateStr + "T00:00:00");
  return d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}

function formatPrice(price) {
  if (price == null) return "—";
  return "$" + Number(price).toFixed(2);
}

function typeLabel(type) {
  return TYPE_LABELS[type] || type;
}

// A trade with no ticker (private funds, bonds, spin-offs, corporate-transaction cash-outs
// - not everything disclosed is a tradeable public stock) otherwise shows as a bare "Sell
// (Full), $1,001-$15,000" with no way to tell a forced buyout payout from an ordinary sale.
// `tickers` is the ticker -> asset_name lookup (data/tickers.json) for the normal case.
function tradeDetails(trade, tickers) {
  const parts = [];
  const name = trade.ticker ? tickers[trade.ticker] : trade.asset_name;
  if (name) parts.push(escapeHtml(name));
  if (trade.comment) parts.push(`<span class="muted">${escapeHtml(trade.comment)}</span>`);
  return parts.join(" — ") || "—";
}

function typeClass(type) {
  return "type-" + type;
}

function chamberLabel(chamber) {
  if (chamber === "house") return "House";
  if (chamber === "senate") return "Senate";
  return chamber || "—";
}

// Finds the first entry in a `[[date, price], ...]` series (ascending, as prices/*.json
// stores it) on or after targetDateStr, rolling forward up to maxRollForwardDays to land on
// the next real trading day past a weekend/holiday - same convention as
// TickerHistory.price_on_or_after on the Python side, reimplemented here since a trade's
// transaction-date price isn't precomputed anywhere in the export (only its disclosure-date
// price is, as price_at_notification/price_date). Returns null past that window.
function priceOnOrAfter(series, targetDateStr, maxRollForwardDays = 10) {
  const cutoff = new Date(targetDateStr + "T00:00:00");
  cutoff.setDate(cutoff.getDate() + maxRollForwardDays);
  const cutoffStr = cutoff.toISOString().slice(0, 10);
  for (const [date, price] of series) {
    if (date >= targetDateStr) return date <= cutoffStr ? [date, price] : null;
  }
  return null;
}

// Makes a whole <tr> navigate to href on click, without hijacking clicks on an inner <a>
// (e.g. a ticker or member link in the same row) - those should still behave like normal
// links, not get overridden by the row's own navigation.
function makeRowsClickable(tbody, hrefForRow) {
  tbody.querySelectorAll("tr").forEach((tr, i) => {
    const href = hrefForRow(i);
    if (!href) return;
    tr.classList.add("row-link");
    tr.addEventListener("click", (e) => {
      if (e.target.closest("a")) return;
      window.location.href = href;
    });
  });
}

// Makes a <table> sortable by clicking header cells - re-sorts the existing <tbody> rows
// in place using each cell's data-sort-value attribute (falls back to visible text).
function makeSortable(table) {
  const allHeaderCells = Array.from(table.querySelectorAll("thead tr th"));
  const headers = allHeaderCells.filter((th) => th.dataset.sort);
  headers.forEach((th) => {
    // Must be th's real position among ALL header cells, not its index within the
    // data-sort-only `headers` list above - a non-sortable column (e.g. "Details") placed
    // before sortable ones would otherwise shift every later column's lookup off by one,
    // silently sorting the wrong <td> (confirmed in politician.html/ticker.html).
    const colIndex = allHeaderCells.indexOf(th);
    th.addEventListener("click", () => {
      const tbody = table.querySelector("tbody");
      const rows = Array.from(tbody.querySelectorAll("tr"));
      const ascending = th.dataset.sortDir !== "asc";
      headers.forEach((h) => delete h.dataset.sortDir);
      th.dataset.sortDir = ascending ? "asc" : "desc";

      rows.sort((a, b) => {
        const cellA = a.children[colIndex];
        const cellB = b.children[colIndex];
        const va = cellA.dataset.sortValue ?? cellA.textContent.trim();
        const vb = cellB.dataset.sortValue ?? cellB.textContent.trim();
        const na = Number(va), nb = Number(vb);
        const cmp = !isNaN(na) && !isNaN(nb) ? na - nb : String(va).localeCompare(String(vb));
        return ascending ? cmp : -cmp;
      });
      rows.forEach((row) => tbody.appendChild(row));
    });
  });
}

// Site-wide header search (#site-search-input / #site-search-results in every page's <header>)
// - lets a visitor jump straight to any politician or ticker from any page, not just
// index.html's own member search or issuers.html's own issuer search. Loads
// legislators.json + tickers.json lazily, on first keystroke, not on every page load - most
// page views never touch the search box, so there's no point paying for 2 extra fetches on
// every single visit just in case someone uses it.
let _siteSearchDataPromise = null;
function loadSiteSearchData() {
  if (!_siteSearchDataPromise) {
    _siteSearchDataPromise = Promise.all([
      fetchJSON("data/legislators.json"),
      fetchJSON("data/tickers.json"),
    ]).then(([legislators, tickers]) => ({ legislators, tickers }));
  }
  return _siteSearchDataPromise;
}

const SITE_SEARCH_MAX_RESULTS = 8;

function siteSearchMatches(data, query) {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const results = [];
  for (const l of data.legislators) {
    const name = `${l.first_name} ${l.last_name}`;
    if (name.toLowerCase().includes(q)) {
      results.push({ type: "Member", label: name, sub: chamberLabel(l.chamber), href: `politician.html?id=${l.id}` });
    }
  }
  for (const [ticker, name] of Object.entries(data.tickers)) {
    if (ticker.toLowerCase().includes(q) || (name && name.toLowerCase().includes(q))) {
      results.push({ type: "Ticker", label: ticker, sub: name || "", href: `ticker.html?ticker=${encodeURIComponent(ticker)}` });
    }
  }
  // A match at the start of the label (e.g. "Nancy" in "Nancy Pelosi", "AAP" in "AAPL") is
  // almost always what the visitor meant more than a mid-string match - rank those first,
  // stable order within each group otherwise.
  results.sort((a, b) => {
    const aStarts = a.label.toLowerCase().startsWith(q) ? 0 : 1;
    const bStarts = b.label.toLowerCase().startsWith(q) ? 0 : 1;
    return aStarts - bStarts;
  });
  return results.slice(0, SITE_SEARCH_MAX_RESULTS);
}

function initSiteSearch() {
  const input = document.getElementById("site-search-input");
  const resultsEl = document.getElementById("site-search-results");
  if (!input || !resultsEl) return;

  let currentData = null;
  let currentResults = [];

  function renderResults(list) {
    currentResults = list;
    if (list.length === 0) {
      resultsEl.innerHTML = input.value.trim() ? `<div class="no-results">No matches</div>` : "";
    } else {
      resultsEl.innerHTML = list.map((r) => `
        <a href="${r.href}">
          <span>${escapeHtml(r.label)}${r.sub ? ` <span class="muted">— ${escapeHtml(r.sub)}</span>` : ""}</span>
          <span class="result-type">${r.type}</span>
        </a>
      `).join("");
    }
    resultsEl.classList.toggle("open", input.value.trim().length > 0);
  }

  input.addEventListener("input", async () => {
    if (!input.value.trim()) { renderResults([]); return; }
    if (!currentData) {
      try {
        currentData = await loadSiteSearchData();
      } catch (e) {
        resultsEl.innerHTML = `<div class="no-results">Search unavailable</div>`;
        resultsEl.classList.add("open");
        return;
      }
      // The fetch is async - the visitor may have already changed or cleared the box by the
      // time it resolves, so re-check instead of rendering results for a stale query.
      if (!input.value.trim()) { renderResults([]); return; }
    }
    renderResults(siteSearchMatches(currentData, input.value));
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && currentResults.length > 0) {
      window.location.href = currentResults[0].href;
    } else if (e.key === "Escape") {
      input.blur();
      resultsEl.classList.remove("open");
    }
  });

  input.addEventListener("focus", () => {
    if (input.value.trim()) resultsEl.classList.add("open");
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".site-search")) resultsEl.classList.remove("open");
  });
}

initSiteSearch();
