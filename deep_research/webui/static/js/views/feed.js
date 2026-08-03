/* Shared renderer for research-job feed lines (taskbar dialog + previews). */

import { el } from "../dom.js";

export function feedLine(event) {
  switch (event.type) {
    case "phase":
      return el(
        "div",
        { class: "feed-line feed-phase" },
        el("span", { text: event.phase }),
        event.detail ? el("span", { class: "feed-detail", text: ` — ${event.detail}` }) : null,
      );
    case "step":
      return el(
        "div",
        { class: "feed-line feed-step" },
        el("span", { text: event.step }),
        event.detail ? el("span", { class: "feed-detail", text: ` — ${event.detail}` }) : null,
      );
    case "done":
      return el("div", {
        class: "feed-line feed-done",
        text: event.archived
          ? "Report saved to the library."
          : "Report finished, but not archived (is pdl.enabled set?).",
      });
    case "error":
      return el("div", { class: "feed-line feed-error", text: event.error || "Unknown error" });
    case "cancelled":
      return el("div", { class: "feed-line feed-error", text: "The job was cancelled." });
    case "lost":
      return el("div", {
        class: "feed-line feed-error",
        text: "Connection lost — the job is no longer available (server restarted?).",
      });
    default:
      return null;
  }
}
