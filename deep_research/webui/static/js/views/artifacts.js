/* Artifacts view: searchable archive of archived documents (PDFs, HTML, images)
   with per-document delete — the safety net for misclassified documents. */

import { el, clear } from "../dom.js";
import { formatDate } from "../format.js";
import { deleteArtifact, listArtifacts } from "../api.js";
import { confirmDialog } from "./dialog.js";

const PAGE_SIZES = [10, 30, 50, 100];
const KINDS = ["", "pdf", "html", "image", "report"];

export function renderArtifacts(root, searchInput) {
  clear(root);

  const state = {
    q: searchInput.value.trim(),
    kind: "",
    items: [],
    total: 0,
    page: 1,
    pageSize: 30,
    loading: false,
    error: "",
  };

  const kindSelect = el(
    "select",
    { class: "select", "aria-label": "Filter by artifact kind" },
    ...KINDS.map((k) => el("option", { value: k, text: k || "All kinds" })),
  );
  const pageSizeSelect = el(
    "select",
    { class: "select select-sm", "aria-label": "Artifacts per page" },
    ...PAGE_SIZES.map((n) =>
      el("option", { value: String(n), text: `${n} / page`, selected: n === state.pageSize }),
    ),
  );
  const summaryEl = el("div", { class: "filter-summary" });
  const listEl = el("div", { class: "report-list" });
  const statusEl = el("div", { class: "list-status" });
  const pagerEl = el("div", { class: "pager" });

  const sidebar = el(
    "aside",
    { class: "sidebar" },
    el("h2", { class: "panel-title", text: "Filters" }),
    el("label", { class: "field-label", text: "Kind" }),
    kindSelect,
    el("hr", { class: "sidebar-divider" }),
    el("a", { class: "link-btn", href: "#/", text: "← Back to reports" }),
  );
  const main = el(
    "section",
    { class: "list-main" },
    el(
      "div",
      { class: "list-toolbar" },
      summaryEl,
      el(
        "label",
        { class: "page-size-row" },
        el("span", { class: "page-size-label", text: "Per page" }),
        pageSizeSelect,
      ),
    ),
    listEl,
    statusEl,
    pagerEl,
  );
  root.append(el("div", { class: "layout" }, sidebar, main));

  let requestSeq = 0;

  function resetAndLoad() {
    state.page = 1;
    load();
  }

  const onSearch = (event) => {
    state.q = event.detail;
    resetAndLoad();
  };
  document.addEventListener("dr:search", onSearch);

  kindSelect.addEventListener("change", () => {
    state.kind = kindSelect.value;
    resetAndLoad();
  });
  pageSizeSelect.addEventListener("change", () => {
    state.pageSize = Number(pageSizeSelect.value) || 30;
    resetAndLoad();
  });

  function cleanup() {
    document.removeEventListener("dr:search", onSearch);
  }

  async function load() {
    const seq = ++requestSeq;
    const offset = (state.page - 1) * state.pageSize;
    state.loading = true;
    state.error = "";
    render();
    try {
      const body = await listArtifacts({
        limit: state.pageSize,
        offset,
        q: state.q || undefined,
        kind: state.kind || undefined,
      });
      if (seq !== requestSeq) return;
      state.items = body.items;
      state.total = body.total;
    } catch (err) {
      if (seq !== requestSeq) return;
      state.error = err.message;
    } finally {
      if (seq === requestSeq) {
        state.loading = false;
        render();
      }
    }
  }

  async function removeArtifact(item) {
    const ok = await confirmDialog({
      title: "Delete document",
      message: `Delete "${item.title || item.artifact_id}" and its analysis from the personal library? This cannot be undone.`,
      confirmText: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      await deleteArtifact(item.artifact_id);
      document.dispatchEvent(new CustomEvent("dr:library"));
      load();
    } catch (err) {
      window.alert(`Delete failed: ${err.message}`);
    }
  }

  function cardEl(item) {
    const badge = el("span", { class: `badge badge-artifact`, text: item.kind || "unknown" });
    const meta = el("div", { class: "card-meta" });
    if (item.relevance_score !== null && item.relevance_score !== undefined) {
      meta.append(el("span", { class: "rel-score", text: `relevance ${item.relevance_score.toFixed(2)}` }));
    }
    meta.append(el("span", { text: formatDate(item.first_seen_at) }));
    const link = item.source_url
      ? el("a", {
          class: "link-btn",
          href: item.source_url,
          target: "_blank",
          rel: "noopener noreferrer",
          text: item.source_url,
        })
      : null;
    const subtitle = item.arxiv_id ? `arxiv:${item.arxiv_id}` : null;
    return el(
      "article",
      { class: "card" },
      el("div", { class: "card-title-row" }, el("h3", { class: "card-title", text: item.title || item.artifact_id }), badge),
      meta,
      subtitle ? el("p", { class: "card-snippet", text: subtitle }) : null,
      link ? el("p", { class: "card-snippet" }, link) : null,
      item.summary ? el("p", { class: "card-snippet", text: item.summary }) : null,
      el(
        "div",
        { class: "card-actions" },
        el("button", {
          class: "btn btn-sm btn-danger",
          type: "button",
          text: "Delete",
          title: "Delete this document (PDF/HTML + analysis) from the personal library",
          onclick: () => removeArtifact(item),
        }),
      ),
    );
  }

  function renderPager() {
    clear(pagerEl);
    if (!state.items.length) return;
    const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
    if (totalPages < 2) return;
    pagerEl.append(
      el("button", {
        class: "pager-btn",
        type: "button",
        text: "‹ Prev",
        disabled: state.loading || state.page === 1,
        onclick: () => {
          state.page -= 1;
          load();
        },
      }),
    );
    pagerEl.append(
      el("span", { class: "summary-text", text: `${state.page} / ${totalPages}` }),
    );
    pagerEl.append(
      el("button", {
        class: "pager-btn",
        type: "button",
        text: "Next ›",
        disabled: state.loading || state.page >= totalPages,
        onclick: () => {
          state.page += 1;
          load();
        },
      }),
    );
  }

  function render() {
    clear(summaryEl);
    clear(listEl);
    clear(statusEl);
    clear(pagerEl);

    if (state.items.length) {
      const first = (state.page - 1) * state.pageSize + 1;
      const last = Math.min(state.total, state.page * state.pageSize);
      summaryEl.append(
        el("span", { class: "summary-text", text: `Showing ${first}–${last} of ${state.total} documents` }),
      );
    }
    if (state.q || state.kind) {
      summaryEl.append(
        el("button", {
          class: "link-btn",
          type: "button",
          text: "Clear filters",
          onclick: () => {
            state.q = "";
            state.kind = "";
            searchInput.value = "";
            kindSelect.value = "";
            resetAndLoad();
          },
        }),
      );
    }

    if (state.error) {
      statusEl.append(el("p", { class: "error", text: state.error }));
    } else if (state.loading && !state.items.length) {
      statusEl.append(el("p", { class: "hint", text: "Loading…" }));
    } else if (!state.items.length) {
      statusEl.append(el("p", { class: "hint", text: "No documents match your filters." }));
    }

    for (const item of state.items) listEl.append(cardEl(item));
    renderPager();
  }

  load();
  return cleanup;
}
