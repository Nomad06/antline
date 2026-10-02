import { html, useEffect, useState } from "../vendor/htm-preact.mjs";
import { get } from "../api.js";
import { Badge, Notice } from "../ui/controls.js";

export function HomePage() {
  const [setup, setSetup] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    get("/api/setup").then(setSetup, (e) => setError(e.message));
  }, []);
  if (error) {
    return html`<${Notice} kind="error">${error}<//>`;
  }
  if (!setup) {
    return html`<p class="muted">Loading...</p>`;
  }
  const done = setup.steps.filter((step) => step.state === "done").length;
  return html`<section class="card">
    <h1>Welcome to antline</h1>
    <p>${done} of ${setup.steps.length} setup steps are done.</p>
    <ol>
      ${setup.steps.map((step) => html`<li><strong>${step.name}</strong> ${step.intro} <${Badge} kind=${step.state}>${step.state}<//></li>`)}
    </ol>
    ${setup.next ? html`<a class="button primary" href="#/setup">Continue setup</a>` : html`<p>Setup is complete.</p>`}
    <p class="muted">Data root <code>${setup.data_root}</code>, code root <code>${setup.code_root || "unknown"}</code>.</p>
  </section>`;
}
