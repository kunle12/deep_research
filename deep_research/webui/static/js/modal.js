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
  function onKey(event) {
    if (event.key === "Escape") (onEscape || close)();
  }
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) (onOverlay || close)();
  });
  document.addEventListener("keydown", onKey);
  open.add(close);
  return { overlay, close };
}
