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
