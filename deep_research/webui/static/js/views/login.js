/* Auth view: sign in, create account (with email verification), password reset.
 *
 * Routes: #/login, #/verify?email=…&token=…, #/reset?email=…&token=…
 * Steps on the login card: signin → register → verify.
 * On success the server sets the session cookie; we dispatch `dr:auth` so the
 * topbar account chip updates, then navigate to the library.
 */

import { el, clear } from "../dom.js";
import {
  login,
  register,
  verifyEmail,
  resendVerification,
  requestPasswordReset,
  resetPassword,
} from "../api.js";

function goLibrary() {
  document.dispatchEvent(new CustomEvent("dr:auth"));
  if (window.location.hash !== "#/") {
    window.location.hash = "#/";
  } else {
    // Same-route navigation doesn't fire hashchange; re-render manually.
    window.dispatchEvent(new Event("hashchange"));
  }
}

function formRow(labelText, input) {
  return el(
    "div",
    { class: "auth-field" },
    el("label", { class: "field-label", text: labelText }),
    input,
  );
}

function errorBox() {
  const box = el("div", { class: "auth-error", role: "alert", hidden: true });
  return {
    node: box,
    show(msg) {
      box.textContent = msg;
      box.hidden = false;
    },
    hide() {
      box.hidden = true;
    },
  };
}

async function guard(form, err, action) {
  err.hide();
  const btn = form.querySelector("button[type=submit]");
  if (btn) btn.disabled = true;
  try {
    await action();
  } catch (e) {
    // Server errors arrive as "status: detail".
    err.show(String(e.message || e).replace(/^\d+:\s*/, ""));
  } finally {
    if (btn) btn.disabled = false;
  }
}

function textField({ type = "text", value = "", placeholder = "", autocomplete = "off", required = true, maxlength }) {
  return el("input", {
    class: "text-input",
    type,
    value,
    placeholder,
    autocomplete,
    required,
    maxlength,
  });
}

export function renderAuth(root, mode, params = {}) {
  clear(root);
  const wrap = el("div", { class: "auth-wrap" });
  const card = el("div", { class: "auth-card" });
  wrap.append(card);
  root.append(wrap);

  if (mode === "reset") {
    renderResetCard(card, params);
  } else {
    renderLoginCard(card, mode, params);
  }
}

function renderLoginCard(card, mode, params) {
  const title = el("h1", { class: "auth-title" });
  const err = errorBox();
  const form = el("form", { class: "auth-form" });
  const alt = el("div", { class: "auth-alt" });
  let step = mode === "verify" ? "verify" : "signin";

  const email = textField({ type: "email", value: params.email || "", placeholder: "you@example.com", autocomplete: "email" });
  const name = textField({ required: false, placeholder: "Optional", maxlength: "64" });
  const password = textField({ type: "password", autocomplete: "current-password" });
  const token = textField({
    value: params.token || "",
    placeholder: "Paste the token from your email",
    autocomplete: "one-time-code",
  });
  const notice = el("p", { class: "auth-notice", hidden: true });
  const submit = el("button", { class: "btn btn-primary auth-submit", type: "submit", text: "Sign in" });

  function showSignin() {
    step = "signin";
    title.textContent = "Sign in";
    err.hide();
    notice.hidden = true;
    email.disabled = false;
    password.disabled = false;
    password.autocomplete = "current-password";
    submit.textContent = "Sign in";
    form.replaceChildren(
      formRow("Email", email),
      formRow("Password", password),
      err.node,
      submit,
    );
    alt.replaceChildren(
      el("button", { class: "linklike", type: "button", text: "Create account", onclick: showRegister }),
      " · ",
      el("button", { class: "linklike", type: "button", text: "Forgot password?", onclick: () => { window.location.hash = "#/reset"; } }),
    );
  }

  function showRegister() {
    step = "register";
    title.textContent = "Create account";
    err.hide();
    notice.hidden = true;
    email.disabled = false;
    name.disabled = false;
    password.disabled = false;
    password.autocomplete = "new-password";
    submit.textContent = "Create account";
    form.replaceChildren(
      formRow("Email", email),
      formRow("Display name", name),
      formRow("Password (min 8 characters)", password),
      err.node,
      submit,
    );
    alt.replaceChildren(
      el("button", { class: "linklike", type: "button", text: "Back to sign in", onclick: showSignin }),
    );
  }

  function showVerify({ lockEmail }) {
    step = "verify";
    title.textContent = "Verify your email";
    err.hide();
    email.disabled = lockEmail;
    name.disabled = true;
    password.disabled = true;
    name.required = false;
    password.required = false;
    submit.textContent = "Verify & sign in";
    const resendBtn = el("button", {
      class: "linklike",
      type: "button",
      text: "Resend token",
      onclick: (event) => {
        event.preventDefault();
        resendVerification(email.value.trim())
          .then(() => { notice.hidden = false; notice.textContent = "A new token was sent."; })
          .catch((e) => err.show(String(e.message || e).replace(/^\d+:\s*/, "")));
      },
    });
    form.replaceChildren(
      formRow("Email", email),
      formRow("Verification token", token),
      resendBtn,
      notice,
      err.node,
      submit,
    );
    alt.replaceChildren(
      el("button", { class: "linklike", type: "button", text: "Back to sign in", onclick: showSignin }),
    );
    token.focus();
  }

  form.onsubmit = (event) => {
    event.preventDefault();
    if (step === "signin") {
      guard(form, err, async () => {
        await login(email.value.trim(), password.value);
        goLibrary();
      });
    } else if (step === "register") {
      guard(form, err, async () => {
        // The response is identical whether or not the account existed
        // (no user enumeration): either way a token email is on its way —
        // a new verification, or a "someone tried to register" notice to
        // the existing owner.
        await register(email.value.trim(), password.value, name.value.trim());
        showVerify({ lockEmail: true });
        notice.hidden = false;
        notice.textContent =
          "Check your email for the verification token" +
          " (self-hosted without SMTP: it is printed in the server log).";
      });
    } else {
      guard(form, err, async () => {
        await verifyEmail(email.value.trim(), token.value.trim());
        goLibrary();
      });
    }
  };

  card.append(title, form, alt);
  if (step === "verify") showVerify({ lockEmail: false });
  else showSignin();
}

function renderResetCard(card, params) {
  const title = el("h1", { class: "auth-title", text: "Reset password" });
  const err = errorBox();
  const form = el("form", { class: "auth-form" });

  const email = textField({ type: "email", value: params.email || "", autocomplete: "email" });
  const token = textField({ value: params.token || "", placeholder: "Token from the reset email" });
  const password = textField({ type: "password", autocomplete: "new-password" });
  const notice = el("p", {
    class: "auth-notice",
    text: "Enter your email, request a reset token, then set a new password.",
  });
  const requestBtn = el("button", {
    class: "btn",
    type: "button",
    text: "Email me a reset token",
    onclick: () => {
      guard(form, err, async () => {
        await requestPasswordReset(email.value.trim());
        notice.textContent =
          "If the account exists, a reset token was sent" +
          " (self-hosted without SMTP: check the server log).";
        notice.hidden = false;
      });
    },
  });

  form.append(
    formRow("Email", email),
    requestBtn,
    notice,
    formRow("Reset token", token),
    formRow("New password (min 8 characters)", password),
    err.node,
    el("button", { class: "btn btn-primary auth-submit", type: "submit", text: "Set new password" }),
  );
  form.onsubmit = (event) => {
    event.preventDefault();
    guard(form, err, async () => {
      await resetPassword(email.value.trim(), token.value.trim(), password.value);
      goLibrary();
    });
  };

  card.append(
    title,
    form,
    el(
      "div",
      { class: "auth-alt" },
      el("button", { class: "linklike", type: "button", text: "Back to sign in", onclick: () => { window.location.hash = "#/login"; } }),
    ),
  );
}
