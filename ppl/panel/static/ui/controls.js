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

function short(count) {
  if (count < 1000) return String(Math.round(count));
  if (count < 1e6) return count < 1e4 ? (count / 1000).toFixed(1) + "k" : Math.round(count / 1000) + "k";
  return count < 1e7 ? (count / 1e6).toFixed(1) + "M" : Math.round(count / 1e6) + "M";
}

export function Measured({ stat }) {
  const line = (text) => html`<span class="help"><br />${text}</span>`;
  if (!stat || !stat.runs) return line("no measured runs yet");
  const head = "last " + (stat.runs === 1 ? "1 run" : stat.runs + " runs") + ": " + short(Math.round(stat.tokens_mean)) + " tokens";
  if (stat.usd_mean === null || stat.usd_mean === undefined) {
    const why = (stat.unpriced && stat.unpriced.length) ? "no price for " + stat.unpriced.join(", ") : (stat.usd_reason || "no reason recorded");
    return line(head + ", cost undefined: " + why);
  }
  return line(head + ", " + (stat.usd_mean < 0.005 ? "<$0.01" : "$" + stat.usd_mean.toFixed(2)) + " per task");
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
  return html`<button class=${(primary ? "primary" : "") + (busy ? " busy" : "")} onClick=${onClick} disabled=${disabled || busy}>
    ${busy ? "Working..." : children}
  </button>`;
}
