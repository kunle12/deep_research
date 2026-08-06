/* Reusable confirmation modal that matches the UI style.
 *
 * Replaces the native window.confirm() for destructive or significant actions
 * (delete report, merge reports) so the dialog looks like the rest of the app.
 * Returns a Promise<boolean> resolving to the user's choice.
 */

import { el } from "../dom.js";

let current = null; // close() of the open dialog, if any

export function confirmDialog({ title, message, confirmText = "Confirm", cancelText = "Cancel", danger = false }) {
  return new Promise((resolve) => {
    if (current) current.close();
    const opener = document.activeElement;
    const overlay = el("div", { class: "modal-overlay confirm-overlay" });
    const actions = el("div", { class: "modal-actions" });
    const dialog = el(
      "div",
      {
        class: "modal confirm-modal",
        role: "dialog",
        "aria-modal": "true",
        "aria-labelledby": "confirm-title",
        "aria-describedby": "confirm-message",
      },
      el("h2", { id: "confirm-title", class: "modal-title", text: title }),
      el("p", { id: "confirm-message", class: "confirm-message", text: message }),
      actions,
    );
    overlay.append(dialog);
    document.body.append(overlay);

    let settled = false;
    function settle(value) {
      if (settled) return;
      settled = true;
      close();
      resolve(value);
    }

    actions.append(
      el("button", {
        class: "btn",
        type: "button",
        text: cancelText,
        onclick: () => settle(false),
      }),
      el("button", {
        class: danger ? "btn btn-danger" : "btn btn-primary",
        type: "button",
        text: confirmText,
        onclick: () => settle(true),
      }),
    );

    function onKey(event) {
      if (event.key === "Escape") settle(false);
    }

    function close() {
      document.removeEventListener("keydown", onKey);
      overlay.remove();
      if (opener && opener.isConnected && opener.focus) opener.focus();
      if (current && current.close === close) current = null;
    }

    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) settle(false);
    });
    document.addEventListener("keydown", onKey);

    current = { close };
  });
}
