/* Report view: rendered markdown, TOC, references panel, tags, delete. */

import { el, clear } from "../dom.js";
import { formatDate, plural } from "../format.js";
import {
  addReportTag,
  deleteReportReference,
  deleteReport,
  fetchArxivPdf,
  getReport,
  listReports,
  mergeReports,
  removeReportTag,
  renameReport,
  startResearch,
} from "../api.js";
import { parse, renderBlocks, safeUrl } from "../markdown.js";
import { hasActiveJob, trackJob } from "../jobs.js";
import { confirmDialog } from "./dialog.js";

export function renderReport(root, runId) {
  clear(root);

  const bar = el(
    "div",
    { class: "report-progress", "aria-hidden": "true" },
    el("div", { class: "report-progress-bar" }),
  );
  document.body.append(bar);

  const onScroll = () => {
    const doc = document.documentElement;
    const max = doc.scrollHeight - window.innerHeight;
    const progress = max > 0 ? Math.min(1, Math.max(0, window.scrollY / max)) : 0;
    bar.firstElementChild.style.transform = `scaleX(${progress})`;
  };
  window.addEventListener("scroll", onScroll, { passive: true });

  const content = el("div", { class: "report-loading", text: "Loading report…" });
  root.append(content);

  getReport(runId)
    .then((report) => {
      clear(content);
      // Drop the loading-state class so its `text-align: center` no longer
      // leaks into the rendered report (bibliography/glossary/panels).
      content.className = "";
      content.append(buildReport(report));
    })

    .catch((err) => {
      clear(content);
      content.append(
        el(
          "div",
          { class: "placeholder" },
          el("p", { class: "error", text: err.message }),
          el("a", { href: "#/", text: "← Back to library" }),
        ),
      );
    });

  return () => {
    window.removeEventListener("scroll", onScroll);
    bar.remove();
  };
}

function buildReport(report) {
  const header = el(
    "header",
    { class: "report-header" },
    el("a", { class: "link-btn", href: "#/", text: "← Library" }),
    el("h1", { class: "report-title", text: report.query || report.run_id }),
    el(
      "div",
      { class: "report-meta" },
      el("span", { class: `badge badge-${report.path}`, text: report.path || "unknown" }),
      el("span", { text: formatDate(report.started_at) }),
      report.iterations !== null && report.iterations !== undefined
        ? el("span", { text: plural(report.iterations, "iteration") })
        : null,
      el("span", { text: plural(report.citations.length, "reference") }),
    ),
    el(
      "div",
      { class: "report-actions" },
      el("button", {
        class: "btn",
        type: "button",
        text: "Rename",
        title: "Rename this research",
        onclick: () => promptRename(report),
      }),
      el("button", {
        class: "btn",
        type: "button",
        text: "Add source",
        title: "Analyze a new URL and attach it to this research",
        onclick: () => openAddSource(report),
      }),
      report.has_pdf
        ? el("a", { class: "btn", href: report.pdf_url, target: "_blank", rel: "noopener", text: "Open PDF" })
        : null,
      el("a", {
        class: "btn",
        href: report.markdown_url,
        download: `${report.run_id}.md`,
        text: "Download .md",
      }),


    ),
  );

  const body = el("div", { class: "md-body" }, ...renderBlocks(parse(report.markdown)));
  stripBibliography(body);

  const headings = [...body.querySelectorAll("[data-heading]")];
  const toc = el("nav", { class: "toc", "aria-label": "Table of contents" });
  if (headings.length >= 3) {
    toc.append(el("h2", { class: "panel-title", text: "Contents" }));
    const list = el("ul", { class: "toc-list" });
    for (const h of headings) {
      const level = Number(h.tagName.slice(1));
      list.append(
        el(
          "li",
          {},
          el("button", {
            class: `toc-link level-${level}`,
            type: "button",
            text: h.textContent.trim(),
            onclick: () => h.scrollIntoView({ behavior: "smooth", block: "start" }),
          }),
        ),
      );
    }
    toc.append(list);
  }

  const article = el(
    "article",
    { class: "report-article" },
    header,
    el("div", { class: "report-content" }, body),
  );

  const refs = el(
    "aside",
    { class: "refs-panel" },
    el(
      "div",
      { class: "panel" },
      el("h2", { class: "panel-title", text: `References (${report.citations.length})` }),
      refsList(report.citations, report.run_id),
      el(
        "div",
        { class: "panel-export" },
        el("a", {
          class: "btn",
          href: report.bibliography_bib_url,
          download: `${report.run_id}.bib`,
          text: "Download .bib",
          title: "Download the report references as BibTeX",
        }),
        el("a", {
          class: "btn",
          href: report.bibliography_url,
          download: `${report.run_id}-bibliography.md`,
          text: "Download .md",
          title: "Download the report bibliography as Markdown",
        }),
      ),
    ),
    glossaryPanel(report),


    tagEditor(report),
    mergePanel(report),
    el(
      "div",
      { class: "panel" },
      el("h2", { class: "panel-title", text: "Danger zone" }),
      el("button", {
        class: "btn btn-danger",
        type: "button",
        text: "Delete report",
        onclick: () => confirmDelete(report),
      }),
    ),
  );

  return el("div", { class: "layout report-layout" }, toc, article, refs);
}

function stripBibliography(container) {
  const headings = [...container.querySelectorAll("h2")];
  const target = headings.find((h) => h.textContent.trim().toLowerCase() === "bibliography");
  if (!target) return;
  let node = target;
  while (node) {
    const next = node.nextElementSibling;
    node.remove();
    if (next && /^H[1-6]$/.test(next.tagName)) break;
    node = next;
  }
}

function glossaryPanel(report) {
  const glossary = report.glossary || [];
  const panel = el("div", { class: "panel" });
  panel.append(el("h2", { class: "panel-title", text: `Glossary (${glossary.length})` }));
  if (!glossary.length) {
    panel.append(el("p", { class: "hint", text: "No glossary terms recorded for this report." }));
    return panel;
  }
  const list = el("div", { class: "glossary-list" });
  for (const g of glossary) {
    const item = el("div", { class: "glossary-item" });
    const head = el("div", { class: "glossary-term" }, el("strong", { text: g.term }));
    if (g.kind) head.append(el("span", { class: "badge", text: g.kind }));
    if (g.acronym_expansion) head.append(el("span", { class: "glossary-exp", text: g.acronym_expansion }));
    item.append(head);
    if (g.short_def) item.append(el("div", { class: "glossary-def", text: g.short_def }));
    if (g.domain_tags && g.domain_tags.length) {
      item.append(el("div", { class: "glossary-tags", text: g.domain_tags.map((t) => `#${t}`).join(" ") }));
    }
    list.append(item);
  }
  panel.append(list);
  panel.append(
    el(
      "div",
      { class: "panel-export" },
      el("a", {
        class: "btn",
        href: report.glossary_url,
        download: `${report.run_id}-glossary.md`,
        text: "Download glossary (.md)",
      }),
    ),
  );
  return panel;
}


function refsList(citations, runId) {
  const list = el("div", { class: "ref-list" });
  if (!citations.length) {
    list.append(el("p", { class: "hint", text: "No references recorded for this report." }));
    return list;
  }
  for (const citation of citations) {
    list.append(refCard(citation, runId));
  }
  return list;
}

function refCard(citation, runId) {
  const title = citation.title || citation.url;
  const metaParts = [];
  if (Array.isArray(citation.authors) && citation.authors.length) {
    metaParts.push(citation.authors.slice(0, 4).join(", ") + (citation.authors.length > 4 ? " et al." : ""));
  }
  if (citation.year) metaParts.push(String(citation.year));
  if (citation.venue) metaParts.push(citation.venue);

  const actionsEl = el("div", { class: "ref-actions" });

  function renderActions() {
    clear(actionsEl);
    const actions = [];
    // Scholar-only hits carry a synthetic "scholar:<hash>" id — they are real
    // papers but have no arXiv record, so never render arXiv-style buttons.
    const isArxiv =
      Boolean(citation.arxiv_id) && !String(citation.arxiv_id).startsWith("scholar:");
    if (isArxiv) {
      const local = Boolean(citation.local_pdf_url);
      if (local) {
        actions.push(refLink("arXiv PDF", citation.local_pdf_url, "Archived PDF copy in library"));
      } else {
        actions.push(refLink("arXiv", `https://arxiv.org/abs/${citation.arxiv_id}`, "View abstract on arXiv.org"));
        actions.push(downloadPdfButton(citation, renderActions));
      }
    }
    if (citation.pdf_url) actions.push(refLink("PDF", citation.pdf_url));
    if (citation.doi) actions.push(refLink("DOI", `https://doi.org/${citation.doi}`));
    // Hide the URL button only when it duplicates the arXiv button itself
    // (same abs page). When a local PDF exists, arXiv opens the local copy
    // and URL remains the official abstract page.
    const urlDuplicatesArxiv =
      isArxiv && !citation.local_pdf_url && /arxiv\.org\/abs\//i.test(citation.url || "");
    if (citation.url && !urlDuplicatesArxiv) {
      actions.push(refLink("URL", citation.url));
    }
    actions.push(deleteReferenceButton(citation, runId));
    for (const action of actions) actionsEl.append(action);
  }

  renderActions();

  return el(
    "div",
    { class: "ref-card" },
    el("h3", { class: "ref-title", text: title }),
    metaParts.length ? el("p", { class: "ref-meta", text: metaParts.join(" · ") }) : null,
    refSnippet(citation.snippet),
    actionsEl,
  );
}

function refSnippet(snippet) {
  if (!snippet) return null;
  const btn = el("button", {
    class: "ref-snippet",
    type: "button",
    title: "Click to expand / collapse the abstract",
    "aria-expanded": "false",
    onclick: () => {
      const expanded = btn.classList.toggle("ref-snippet-expanded");
      btn.setAttribute("aria-expanded", expanded ? "true" : "false");
    },
  });
  btn.append(el("span", { text: snippet }));
  return btn;
}

function deleteReferenceButton(citation, runId) {
  let btn = null;
  async function onClick() {
    const ok = await confirmDialog({
      title: "Delete reference",
      message: `Delete "${citation.title || citation.url}" from this report? Its bibliography entry (and .bib export) will be removed, and any archived copy in your personal library will be deleted. This cannot be undone.`,
      confirmText: "Delete",
      danger: true,
    });
    if (!ok) return;
    btn.disabled = true;
    btn.textContent = "Deleting…";
    try {
      await deleteReportReference(runId, citation.url, citation.arxiv_id);
      document.dispatchEvent(new CustomEvent("dr:library"));
      window.location.reload();
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "Delete";
      window.alert(`Delete failed: ${err.message}`);
    }
  }
  btn = el("button", {
    class: "btn btn-sm btn-danger",
    type: "button",
    text: "Delete",
    title: "Remove this reference from the report and clean up its bibliography / archived copy",
    onclick: onClick,
  });
  return btn;
}

function downloadPdfButton(citation, rerender) {
  let btn = null;
  async function onClick() {
    btn.disabled = true;
    btn.textContent = "Downloading…";
    try {
      const res = await fetchArxivPdf(citation.arxiv_id);
      if (res.local_pdf_url) {
        citation.local_pdf_url = res.local_pdf_url;
        rerender();
        return;
      }
      btn.textContent = "Retry";
      btn.title = res.error || "Download failed";
    } catch (err) {
      btn.textContent = "Retry";
      btn.title = err.message;
    }
    btn.disabled = false;
  }
  btn = el("button", {
    class: "btn btn-sm",
    type: "button",
    text: "Get PDF",
    title: "Download and archive this paper's PDF",
    onclick: onClick,
  });
  return btn;
}

function refLink(label, url, title) {
  return el("a", { class: "btn btn-sm", href: safeUrl(url), target: "_blank", rel: "noopener noreferrer", title, text: label });
}

function tagEditor(report) {
  const chipsWrap = el("div", { class: "tag-cloud" });
  const input = el("input", { class: "search-input", type: "text", placeholder: "Add a tag…", "aria-label": "Add a tag" });
  const statusEl = el("p", { class: "hint", role: "status" });

  function renderTags() {
    clear(chipsWrap);
    if (!report.tags.length) {
      chipsWrap.append(el("p", { class: "hint", text: "No tags yet." }));
      return;
    }
    for (const tag of report.tags) {
      chipsWrap.append(
        el(
          "button",
          {
            class: "chip chip-active",
            type: "button",
            title: `Remove tag ${tag}`,
            onclick: () => removeTag(tag),
          },
          tag,
          el("span", { class: "chip-count", text: "×" }),
        ),
      );
    }
  }

  function setStatus(message) {
    statusEl.textContent = message;
  }

  async function addTag() {
    const tag = input.value.trim();
    if (!tag) return;
    try {
      const result = await addReportTag(report.run_id, tag);
      report.tags = result.tags;
      input.value = "";
      renderTags();
      setStatus("");
    } catch (err) {
      setStatus(err.message);
    }
  }

  async function removeTag(tag) {
    try {
      const result = await removeReportTag(report.run_id, tag);
      report.tags = result.tags;
      renderTags();
      setStatus("");
    } catch (err) {
      setStatus(err.message);
    }
  }

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") addTag();
  });

  const editor = el(
    "div",
    { class: "panel tag-editor" },
    el("h2", { class: "panel-title", text: "Tags" }),
    chipsWrap,
    el("div", { class: "add-row" }, input, el("button", { class: "btn btn-sm", type: "button", text: "Add", onclick: addTag })),
    statusEl,
  );

  renderTags();
  return editor;
}

function confirmDelete(report) {
  confirmDialog({
    title: "Delete report",
    message: `Delete "${report.query || report.run_id}" and its archived files? This cannot be undone.`,
    confirmText: "Delete",
    danger: true,
  }).then((ok) => {
    if (!ok) return;
    deleteReport(report.run_id)
      .then(() => {
        document.dispatchEvent(new CustomEvent("dr:library"));
        window.location.hash = "#/";
      })
      .catch((err) => {
        window.alert(`Delete failed: ${err.message}`);
      });
  });
}

function promptRename(report) {
  const name = window.prompt("New name for this research:", report.query || report.run_id);
  if (name === null) return;
  const trimmed = name.trim();
  if (!trimmed) {
    window.alert("Name cannot be blank.");
    return;
  }
  renameReport(report.run_id, trimmed)
    .then((updated) => {
      report.query = updated.query;
      window.location.reload();
    })
    .catch((err) => {
      window.alert(`Rename failed: ${err.message}`);
    });
}

function openAddSource(report) {
  if (hasActiveJob()) {
    window.alert("A research job is already running — wait for it to finish, then try again.");
    return;
  }
  const url = window.prompt(
    "Enter the URL of the paper / document / blog to analyze and attach to this research:",
    "",
  );
  if (url === null) return;
  const trimmed = url.trim();
  if (!/^https?:\/\//i.test(trimmed)) {
    window.alert("Please enter a valid http(s):// URL.");
    return;
  }
  startResearch(trimmed, "url_source", report.run_id)
    .then((res) => {
      trackJob(res.job_id, trimmed, report.run_id);
      window.alert(
        "Analysis started — track its progress in the bottom taskbar. The report will update when it finishes.",
      );
    })
    .catch((err) => {
      const msg = String(err.message);
      window.alert(
        msg.includes("409")
          ? "A research job is already running — wait for it to finish, then try again."
          : `Failed to start: ${msg}`,
      );
    });
}

function mergePanel(report) {
  const listWrap = el("div", { class: "tag-cloud" });
  const input = el("input", {
    class: "search-input",
    type: "text",
    placeholder: "Search reports to merge…",
    "aria-label": "Search reports to merge",
  });
  const nameInput = el("input", {
    class: "search-input",
    type: "text",
    placeholder: "New merged name (optional)",
    "aria-label": "New merged name",
  });
  const delCheck = el("input", { type: "checkbox", id: "merge-del-src", "aria-label": "Delete sources" });
  const statusEl = el("p", { class: "hint", role: "status" });
  const selected = [];

  function renderSelected() {
    clear(listWrap);
    if (!selected.length) {
      listWrap.append(el("p", { class: "hint", text: "No other reports selected yet." }));
      return;
    }
    for (const item of selected) {
      listWrap.append(
        el(
          "button",
          {
            class: "chip chip-active",
            type: "button",
            title: `Remove ${item.query}`,
            onclick: () => {
              const idx = selected.indexOf(item);
              if (idx !== -1) selected.splice(idx, 1);
              renderSelected();
            },
          },
          item.query,
          el("span", { class: "chip-count", text: "×" }),
        ),
      );
    }
  }

  let searchSeq = 0;
  let currentResults = [];
  async function onSearch(value) {
    const seq = ++searchSeq;
    if (!value.trim()) {
      currentResults = [];
      return;
    }
    try {
      const body = await listReports({ q: value.trim(), limit: 8 });
      if (seq !== searchSeq) return;
      currentResults = body.items.filter((r) => r.run_id !== report.run_id);
    } catch {
      currentResults = [];
    }
  }

  const addBtn = el("button", {
    class: "btn btn-sm",
    type: "button",
    text: "Add",
    onclick: async () => {
      const value = input.value.trim();
      if (!value) return;
      await onSearch(value);
      input.value = "";
      if (!currentResults.length) {
        statusEl.textContent = "No matching reports found.";
        return;
      }
      const match = currentResults[0];
      if (!selected.includes(match)) selected.push(match);
      currentResults = [];
      renderSelected();
      statusEl.textContent = "";
    },
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") addBtn.click();
  });

  const mergeBtn = el("button", {
    class: "btn btn-sm",
    type: "button",
    text: "Merge now",
    onclick: async () => {
      if (!selected.length) {
        statusEl.textContent = "Select at least one other report first.";
        return;
      }
      const mergedName = nameInput.value.trim() || null;
      const ok = await confirmDialog({
        title: "Merge reports",
        message: delCheck.checked
          ? `Merge this report with ${selected.length} other report${selected.length > 1 ? "s" : ""} into one? The source reports will be deleted after their results are merged.`
          : `Merge this report with ${selected.length} other report${selected.length > 1 ? "s" : ""} into one unified report? The originals will be kept and tagged "merged".`,
        confirmText: "Merge",
        danger: delCheck.checked,
      });
      if (!ok) return;
      mergeBtn.disabled = true;
      statusEl.textContent = "Merging…";
      try {
        const res = await mergeReports(
          report.run_id,
          selected.map((r) => r.run_id),
          mergedName,
          delCheck.checked,
        );
        // Source reports may have been deleted (delete_sources) or kept and
        // re-tagged; either way the library totals changed.
        document.dispatchEvent(new CustomEvent("dr:library"));
        window.location.hash = `#/report/${encodeURIComponent(res.run_id)}`;
      } catch (err) {
        mergeBtn.disabled = false;
        statusEl.textContent = err.message;
      }
    },
  });

  renderSelected();
  return el(
    "div",
    { class: "panel merge-panel" },
    el("h2", { class: "panel-title", text: "Merge with another report" }),
    el(
      "label",
      { class: "field-label", text: "Search" },
      el("div", { class: "add-row" }, input, addBtn),
    ),
    listWrap,
    el("label", { class: "field-label", text: "Merged name" }, nameInput),
    el("label", { class: "check-row" }, delCheck, el("span", { text: "Delete source reports" })),
    el("div", { class: "add-row" }, mergeBtn),
    statusEl,
  );
}
