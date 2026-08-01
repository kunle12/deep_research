/* New research modal: start a job, stream SSE progress, cancel, open result. */

import { el, clear } from "../dom.js";
import { cancelJob, getJob, startResearch, researchStreamUrl } from "../api.js";

const PATHS = ["quick", "deep", "academic", "url_source"];
const TERMINAL = new Set(["done", "error", "cancelled"]);

export function openResearchModal() {
  const overlay = el("div", { class: "modal-overlay" });
  const dialog = el("div", {
    class: "modal",
    role: "dialog",
    "aria-modal": "true",
    "aria-labelledby": "research-title",
  });

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
  const startBtn = el("button", {
    class: "btn btn-primary",
    type: "button",
    text: "Start research",
    disabled: true,
  });
  const closeBtn = el("button", { class: "btn", type: "button", text: "Close" });
  const cancelBtn = el("button", {
    class: "btn",
    type: "button",
    text: "Cancel research",
    hidden: true,
  });
  const statusEl = el("div", { class: "research-status", role: "status" });
  const feedEl = el("div", { class: "research-feed", "aria-live": "polite" });

  const form = el(
    "form",
    { class: "research-form" },
    el("label", { class: "field-label", text: "Query" }),
    query,
    el("label", { class: "field-label", text: "Path" }),
    pathSelect,
    el("div", { class: "modal-actions" }, startBtn, cancelBtn, closeBtn),
  );
  dialog.append(
    el("h2", { id: "research-title", class: "modal-title", text: "New research" }),
    form,
    statusEl,
    feedEl,
  );
  overlay.append(dialog);
  document.body.append(overlay);

  let es = null;
  let jobId = null;
  let running = false;
  let reconnectChecked = false;

  function setRunning(value) {
    running = value;
    startBtn.disabled = value || !query.value.trim();
    query.disabled = value;
    pathSelect.disabled = value;
    cancelBtn.hidden = !value;
    closeBtn.hidden = value;
  }

  function close() {
    if (es) {
      es.close();
      es = null;
    }
    document.removeEventListener("keydown", onKey);
    overlay.remove();
  }

  function onKey(event) {
    if (event.key === "Escape" && !running) close();
  }
  document.addEventListener("keydown", onKey);

  function appendFeed(line) {
    feedEl.append(line);
    feedEl.scrollTop = feedEl.scrollHeight;
  }

  function setStatus(text) {
    statusEl.textContent = text;
  }

  function handleEvent(event) {
    switch (event.type) {
      case "phase":
        appendFeed(
          el(
            "div",
            { class: "feed-line feed-phase" },
            el("span", { text: event.phase }),
            event.detail ? el("span", { class: "feed-detail", text: ` — ${event.detail}` }) : null,
          ),
        );
        setStatus("Running…");
        break;
      case "step":
        appendFeed(
          el(
            "div",
            { class: "feed-line feed-step" },
            el("span", { text: event.step }),
            event.detail ? el("span", { class: "feed-detail", text: ` — ${event.detail}` }) : null,
          ),
        );
        break;
      case "done":
        setStatus("Research complete.");
        appendFeed(
          el("div", {
            class: "feed-line feed-done",
            text: event.archived
              ? "Report saved to the library."
              : "Report finished, but not archived (is pdl.enabled set?).",
          }),
        );
        if (event.run_id) {
          appendFeed(el("div", { class: "feed-line feed-done", text: "Opening report…" }));
          setTimeout(() => {
            close();
            window.location.hash = `#/report/${encodeURIComponent(event.run_id)}`;
          }, 900);
        }
        break;
      case "error":
        setStatus("Research failed.");
        appendFeed(el("div", { class: "feed-line feed-error", text: event.error || "Unknown error" }));
        break;
      case "cancelled":
        setStatus("Research cancelled.");
        appendFeed(el("div", { class: "feed-line feed-error", text: "The job was cancelled." }));
        break;
      default:
        break;
    }
  }

  function finishFeed() {
    if (es) {
      es.close();
      es = null;
    }
    setRunning(false);
  }

  function attachStream() {
    es = new EventSource(researchStreamUrl(jobId));
    es.onmessage = (msg) => {
      let event;
      try {
        event = JSON.parse(msg.data);
      } catch {
        return;
      }
      handleEvent(event);
      if (TERMINAL.has(event.type)) finishFeed();
    };
    es.onerror = () => {
      if (!es || reconnectChecked) return;
      reconnectChecked = true;
      setStatus("Connection lost — checking job status…");
      getJob(jobId)
        .then((status) => {
          const type = status.status === "done" ? "done" : status.status === "failed" ? "error" : status.status === "cancelled" ? "cancelled" : null;
          if (type) {
            handleEvent({ type, run_id: status.run_id, archived: status.archived, error: status.error });
            finishFeed();
          } else {
            setStatus("Connection lost — reconnecting…");
          }
        })
        .catch(() => setStatus("Connection lost — the server may be unavailable."));
    };
  }

  async function start() {
    const text = query.value.trim();
    if (!text || running) return;
    setRunning(true);
    reconnectChecked = false;
    clear(feedEl);
    setStatus("Starting…");
    try {
      const res = await startResearch(text, pathSelect.value || null);
      jobId = res.job_id;
      setStatus("Running…");
      attachStream();
    } catch (err) {
      setStatus("Failed to start research.");
      appendFeed(el("div", { class: "feed-line feed-error", text: err.message }));
      setRunning(false);
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    start();
  });
  query.addEventListener("input", () => {
    startBtn.disabled = running || !query.value.trim();
  });
  closeBtn.addEventListener("click", close);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay && !running) close();
  });
  cancelBtn.addEventListener("click", async () => {
    if (!jobId) return;
    cancelBtn.disabled = true;
    setStatus("Cancelling…");
    try {
      const status = await cancelJob(jobId);
      if (status.status !== "running") {
        setStatus(
          status.status === "cancelled"
            ? "Research cancelled."
            : status.status === "failed"
              ? "Research failed."
              : "Research complete.",
        );
      }
    } catch {
      /* the SSE stream / status poll will surface the outcome */
    } finally {
      cancelBtn.disabled = false;
    }
  });

  query.focus();
  return close;
}
