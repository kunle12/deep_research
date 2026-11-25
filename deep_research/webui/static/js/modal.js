/* Shared modal scaffolding and a registry of open modals.
 *
 * Every modal (confirm dialog, job-progress dialog, research modal) is built
 * on the same overlay + Escape + click-to-close + focus-restore skeleton, and
 * registers its close() here so navigation can close every open modal at once.
 * Callers can override the Escape / overlay-click behaviour (e.g. a confirm
 * dialog must settle its Promise on cancel) via onEscape / onOverlay.
 */

import { el } from "./dom.js";

const open = new Set(); // close() of every currently-open modal

export function closeAllModals() {
  for (const close of [...open]) close();
}

export function openModal({ onEscape = null, onOverlay = null, onClose = null } = {}) {
  const opener = document.activeElement;
  const overlay = el("div", { class: "modal-overlay" });
  document.body.append(overlay);

  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey);
    overlay.remove();
    if (opener && opener.isConnected && opener.focus) opener.focus();
    open.delete(close);
    if (onClose) onClose();
  }
  function focusables() {
    return [
      ...overlay.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    ].filter((node) => node.offsetParent !== null);
  }

  function onKey(event) {
    if (event.key === "Escape") {
      (onEscape || close)();
      return;
    }
    // Keep Tab inside the open dialog (the page behind is inert while a
    // modal is up — keyboard shortcuts must not reach background controls).
    if (event.key === "Tab") {
      const items = focusables();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  }
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) (onOverlay || close)();
  });
  document.addEventListener("keydown", onKey);
  open.add(close);
  // Callers append their dialog right after openModal(); move focus into it on
  // the next tick (without stealing focus from an input that already has it).
  setTimeout(() => {
    if (closed) return;
    if (!overlay.contains(document.activeElement)) {
      const [first] = focusables();
      first?.focus();
    }
  }, 0);
  return { overlay, close };
}
