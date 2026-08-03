/* New research modal: query entry only.
 *
 * Starting a job hands it to the global tracker (js/jobs.js) and closes —
 * progress then lives in the bottom taskbar and its detail dialog, so the
 * library stays usable while research runs.
 */

import { el } from "../dom.js";
import { startResearch } from "../api.js";
import { hasActiveJob, trackJob } from "../jobs.js";

const PATHS = ["quick", "deep", "academic", "url_source"];

export function openResearchModal() {
  const overlay = el("div", { class: "modal-overlay" });
  const opener = document.activeElement;
  const dialog = el(
    "div",
    { class: "modal", role: "dialog", "aria-modal": "true", "aria-labelledby": "research-title" },
  );

  const query = el("textarea", {
    class: "research-query",
    rows: 4,
    placeholder: "What would you like to research?",
    "aria-label": "Research query",
  });
  const pathSelect = el(
    "select",
    { class: "select", "aria-label": "Research path" },
    el("option", { value: "", text: "Auto (classifier decides)" }),
    ...PATHS.map((p) => el("option", { value: p, text: p })),
  );
  let busy = hasActiveJob();
  const startBtn = el("button", {
    class: "btn btn-primary",
    type: "submit",
    text: "Start research",
    disabled: true,
  });
  const closeBtn = el("button", { class: "btn", type: "button", text: "Close" });
  const statusEl = el("div", { class: "research-status", role: "status" });
  if (busy) {
    statusEl.textContent = "A research job is already running — track it in the bottom bar.";
  }

  const form = el(
    "form",
    { class: "research-form" },
    el("label", { class: "field-label", text: "Query" }),
    query,
    el("label", { class: "field-label", text: "Path" }),
    pathSelect,
    el("div", { class: "modal-actions" }, startBtn, closeBtn),
  );
  dialog.append(
    el("h2", { id: "research-title", class: "modal-title", text: "New research" }),
    form,
    statusEl,
  );
  overlay.append(dialog);
  document.body.append(overlay);

  let starting = false;
  let closed = false;

  function refreshStart() {
    startBtn.disabled = busy || starting || !query.value.trim();
  }

  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("dr:jobs", onJobs);
    overlay.remove();
    if (opener && opener.isConnected && opener.focus) opener.focus();
  }

  function onKey(event) {
    if (event.key === "Escape" && !starting) close();
  }

  function onJobs() {
    const nowBusy = hasActiveJob();
    if (nowBusy === busy) return;
    busy = nowBusy;
    if (busy) {
      statusEl.textContent = "A research job is already running — track it in the bottom bar.";
    } else if (!starting) {
      statusEl.textContent = "";
    }
    refreshStart();
  }
  document.addEventListener("keydown", onKey);
  document.addEventListener("dr:jobs", onJobs);

  async function start() {
    const text = query.value.trim();
    if (!text || starting || busy) return;
    starting = true;
    refreshStart();
    query.disabled = true;
    pathSelect.disabled = true;
    statusEl.textContent = "Starting…";
    try {
      const res = await startResearch(text, pathSelect.value || null);
      trackJob(res.job_id, text);
      close();
    } catch (err) {
      starting = false;
      query.disabled = false;
      pathSelect.disabled = false;
      statusEl.textContent = String(err.message).includes("409")
        ? "A research job is already running — track it in the bottom bar."
        : `Failed to start research: ${err.message}`;
      refreshStart();
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    start();
  });
  query.addEventListener("input", refreshStart);
  closeBtn.addEventListener("click", close);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay && !starting) close();
  });

  refreshStart();
  query.focus();
  return close;
}
