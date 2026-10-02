import { html } from "../vendor/htm-preact.mjs";

export function Notice({ kind, children }) {
  return html`<div class=${"notice " + (kind || "info")} role=${kind === "error" ? "alert" : "status"}>${children}</div>`;
}

export function Field({ label, help, error, children }) {
  return html`<label class="field">
    <span class="label">${label}</span>
    ${children}
    ${help ? html`<span class="help">${help}</span>` : null}
    ${error ? html`<span class="error">${error}</span>` : null}
  </label>`;
}

export function Badge({ kind, children }) {
  return html`<span class=${"badge " + (kind || "")}>${children}</span>`;
}

export function Log({ lines }) {
  if (!lines || !lines.length) {
    return null;
  }
  return html`<pre class="log">${lines.join("\n")}</pre>`;
}

export function Button({ onClick, busy, primary, disabled, children }) {
  return html`<button class=${primary ? "primary" : ""} onClick=${onClick} disabled=${disabled || busy}>
    ${busy ? "Working..." : children}
  </button>`;
}
