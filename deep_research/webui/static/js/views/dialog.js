/* Reusable confirmation / prompt / notice modals that match the LCARS theme.
 *
 * - confirmDialog: destructive or significant action confirmation.
 * - promptDialog:  single-field text input (rename, add source).
 * - alertDialog:   one-button notice (errors, "analysis started").
 *
 * All return Promises and supersede any previously open themed dialog, so a
 * navigation-triggered close can never leave an awaiting caller hanging.
 */

import { el } from "../dom.js";
import { openModal } from "../modal.js";

let current = null; // { close, settle } of the open dialog, if any

function supersede() {
  if (current) current.settle(null);
}

function shell({ title, cls = "", titleClass = "", body, actions, onCancel }) {
  const titleId = `dlg-${Math.random().toString(36).slice(2, 9)}`;
  const dialog = el(
    "div",
    {
      class: `modal ${cls}`,
      role: "dialog",
      "aria-modal": "true",
      "aria-labelledby": titleId,
    },
    el("h2", { id: titleId, class: `modal-title ${titleClass}`.trim(), text: title }),
    body,
    actions,
  );
  const { overlay, close } = openModal({
    onEscape: () => onCancel(),
    onOverlay: () => onCancel(),
    onClose: () => onCancel(),
  });
  overlay.append(dialog);
  return { overlay, close };
}

export function confirmDialog({ title, message, confirmText = "Confirm", cancelText = "Cancel", danger = false }) {
  return new Promise((resolve) => {
    supersede();

    const actions = el("div", { class: "modal-actions" });
    let settled = false;
    let close = () => {};

    function settle(value) {
      if (settled) return;
      settled = true;
      close();
      resolve(value);
    }

    const body = el("p", { class: "confirm-message", text: message });
    ({ close } = shell({
      title,
      cls: danger ? "confirm-modal danger" : "confirm-modal",
      body,
      actions,
      onCancel: () => settle(false),
    }));

    actions.append(
      el("button", { class: "btn", type: "button", text: cancelText, onclick: () => settle(false) }),
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

export function promptDialog({
  title,
  message = "",
  label = "",
  value = "",
  placeholder = "",
  confirmText = "Save",
  cancelText = "Cancel",
  required = true,
  mono = false,
  validate = null,
}) {
  return new Promise((resolve) => {
    supersede();

    const inputId = `dlg-input-${Math.random().toString(36).slice(2, 9)}`;
    const input = el("input", {
      id: inputId,
      class: mono ? "text-input mono" : "text-input",
      type: "text",
      value,
      placeholder,
      autocomplete: "off",
    });
    const err = el("p", { class: "auth-error", role: "alert", hidden: true });
    const actions = el("div", { class: "modal-actions" });
    let settled = false;
    let close = () => {};

    function showError(msg) {
      err.textContent = msg;
      err.hidden = false;
      input.focus();
    }

    function settle(result) {
      if (settled) return;
      settled = true;
      close();
      resolve(result);
    }

    function submit() {
      const trimmed = input.value.trim();
      if (required && !trimmed) {
        showError("This field cannot be blank.");
        return;
      }
      const problem = validate ? validate(trimmed) : "";
      if (problem) {
        showError(problem);
        return;
      }
      settle(trimmed);
    }

    const body = el(
      "div",
      {},
      message ? el("p", { class: "confirm-message", text: message }) : null,
      label ? el("label", { class: "field-label", for: inputId, text: label }) : null,
      input,
      err,
    );

    ({ close } = shell({
      title,
      cls: "confirm-modal",
      body,
      actions,
      onCancel: () => settle(null),
    }));

    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        submit();
      }
    });

    actions.append(
      el("button", { class: "btn", type: "button", text: cancelText, onclick: () => settle(null) }),
      el("button", { class: "btn btn-primary", type: "button", text: confirmText, onclick: submit }),
    );

    current = { close, settle };
    input.focus();
    input.select?.();
  });
}

export function alertDialog({ title, message, confirmText = "Close" }) {
  return new Promise((resolve) => {
    supersede();

    const actions = el("div", { class: "modal-actions" });
    let settled = false;
    let close = () => {};

    function settle() {
      if (settled) return;
      settled = true;
      close();
      resolve();
    }

    ({ close } = shell({
      title,
      cls: "confirm-modal",
      body: el("p", { class: "confirm-message", text: message }),
      actions,
      onCancel: settle,
    }));

    actions.append(el("button", { class: "btn btn-primary", type: "button", text: confirmText, onclick: settle }));

    current = { close, settle };
  });
}
