/* Reusable confirmation modal that matches the UI style.
 *
 * Replaces the native window.confirm() for destructive or significant actions
 * (delete report, merge reports) so the dialog looks like the rest of the app.
 * Returns a Promise<boolean> resolving to the user's choice.
 */

import { el } from "../dom.js";
import { openModal } from "../modal.js";

let current = null; // { close, settle } of the open dialog, if any

export function confirmDialog({ title, message, confirmText = "Confirm", cancelText = "Cancel", danger = false }) {
  return new Promise((resolve) => {
    // Supersede the previous dialog by *settling* it (not just closing) so its
    // awaiting caller unblocks instead of hanging forever.
    if (current) current.settle(false);

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

    let settled = false;
    function settle(value) {
      if (settled) return;
      settled = true;
      close();
      resolve(value);
    }

    const { overlay, close } = openModal({
      onEscape: () => settle(false),
      onOverlay: () => settle(false),
      // Navigation (closeAllModals) closes via the helper without a settle —
      // resolve false so the awaiting caller never hangs.
      onClose: () => {
        if (!settled) {
          settled = true;
          resolve(false);
        }
      },
    });
    overlay.append(dialog);

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

    current = { close, settle };
  });
}
