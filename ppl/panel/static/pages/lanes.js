import { html, useEffect, useState } from "../vendor/htm-preact.mjs";
import { get } from "../api.js";
import { Badge, Notice } from "../ui/controls.js";

function shown(value) {
  return value === null || value === undefined || value === "" ? "-" : String(value);
}

function dayLine(day) {
  if (!day || day.error) {
    return "Today's spend is unknown" + (day && day.error ? ": " + String(day.error) : "");
  }
  const spent = day.usd === null || day.usd === undefined ? "unknown" : "$" + String(day.usd);
  const cap = day.cap ? " of a $" + String(day.cap) + " cap" : " (no daily cap)";
  return "Today: " + spent + cap;
}

function uncounted(day) {
  const issues = day && day.issues && typeof day.issues === "object" ? day.issues : {};
  return Object.values(issues).reduce((sum, list) => sum + (Array.isArray(list) ? list.length : 0), 0);
}

export function LanesPage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    get("/api/lanes").then(setData, (e) => setError(e.message));
  }, []);
  if (error) {
    return html`<${Notice} kind="error">${error}<//>`;
  }
  if (!data) {
    return html`<p class="muted">Loading...</p>`;
  }
  const missed = uncounted(data.day);
  return html`<section class="lanes">
    <h1>Lanes</h1>
    <p class="lede">${data.lanes.length} of ${shown(data.machine_max)} lanes in use. ${dayLine(data.day)}.${missed ? " " + missed + " runs not counted." : ""}</p>
    ${data.error ? html`<${Notice} kind="error">${String(data.error)}<//>` : null}
    ${data.lanes.length ? html`<table>
      <thead><tr><th>Lane</th><th>Task</th><th>Harness</th><th>Stage</th><th>Window</th><th>Spend</th><th>Resume with</th></tr></thead>
      <tbody>
        ${data.lanes.map((row) => html`<tr>
          <td>${shown(row.lane)}</td>
          <td><a href="/runs">${shown(row.task)}</a></td>
          <td>${shown(row.harness)}</td>
          <td>${shown(row.stage)}${row.outcome ? html` <${Badge}>${String(row.outcome)}<//>` : null}</td>
          <td>${row.alive ? "open" : "closed"}</td>
          <td>${shown(row.usd)}</td>
          <td><code>${shown(row.resume)}</code></td>
        </tr>`)}
      </tbody>
    </table>` : html`<p class="muted">No lane is running. Start one in a terminal with <code>ppl lane KEY-123</code>.</p>`}
  </section>`;
}
