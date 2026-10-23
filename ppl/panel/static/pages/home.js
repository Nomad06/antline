import { html, useEffect, useState } from "../vendor/htm-preact.mjs";
import { get } from "../api.js";
import { Badge, Notice } from "../ui/controls.js";
import { AntTrail } from "../ui/ant.js";

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
  return html`<section class="home">
    <h1>Welcome to antline</h1>
    <p class="lede">${done} of ${setup.steps.length} setup steps are done.</p>
    <${AntTrail} steps=${setup.steps} next=${setup.next} />
    ${setup.next ? html`<a class="button primary" href="#/setup">Continue setup</a>` : html`<p>Setup is complete. Open Claude Code in the workspace and type <code>/pipeline KEY-123</code>.</p>`}
    <ol>
      ${setup.steps.map((step) => html`<li><strong>${step.name}</strong> ${step.intro} <${Badge} kind=${step.state}>${step.state}<//></li>`)}
    </ol>
    <p class="muted">Data root <code>${setup.data_root}</code>, assets directory <code>${setup.code_root || "unknown"}</code>.</p>
  </section>`;
}
