/* Report view: rendered markdown, TOC, references panel, tags, delete. */

import { el, clear } from "../dom.js";
import { formatDate, plural } from "../format.js";
import { addReportTag, deleteReport, fetchArxivPdf, getReport, removeReportTag } from "../api.js";
import { parse, renderBlocks, safeUrl } from "../markdown.js";

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
      refsList(report.citations),
    ),
    tagEditor(report),
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

function refsList(citations) {
  const list = el("div", { class: "ref-list" });
  if (!citations.length) {
    list.append(el("p", { class: "hint", text: "No references recorded for this report." }));
    return list;
  }
  for (const citation of citations) list.append(refCard(citation));
  return list;
}

function refCard(citation) {
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
    for (const action of actions) actionsEl.append(action);
  }

  renderActions();

  return el(
    "div",
    { class: "ref-card" },
    el("h3", { class: "ref-title", text: title }),
    metaParts.length ? el("p", { class: "ref-meta", text: metaParts.join(" · ") }) : null,
    citation.snippet ? el("p", { class: "ref-snippet", text: citation.snippet }) : null,
    actionsEl,
  );
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
  const ok = window.confirm(
    `Delete "${report.query || report.run_id}" and its archived files? This cannot be undone.`,
  );
  if (!ok) return;
  deleteReport(report.run_id)
    .then(() => {
      window.location.hash = "#/";
    })
    .catch((err) => {
      window.alert(`Delete failed: ${err.message}`);
    });
}
