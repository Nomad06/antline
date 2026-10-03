import { html, useEffect, useState } from "../vendor/htm-preact.mjs";
import { get, post } from "../api.js";
import { Badge, Button, Field, Notice } from "../ui/controls.js";

const ROLES = ["ppl-code-worker", "ppl-test-worker", "ppl-briefer", "ppl-brief-challenger",
               "ppl-verifier", "ppl-review-responder", "operator"];
const TOOLS = ["Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Grep", "Glob"];

function examples(doc) {
  const gates = doc && Array.isArray(doc.gate) ? doc.gate : [];
  const focused = gates.find((gate) => gate && gate.name === "focused-tests" && typeof gate.command === "string");
  const test = focused ? focused.command.replace("<test_class>", "app.FooTest").replace("<module>", "core") : "make test";
  return [
    { label: "The code writer edits a test", role: "ppl-code-worker", tool: "Write", value: "src/test/FooTest.java" },
    { label: "The test writer runs one test", role: "ppl-test-worker", tool: "Bash", value: test },
    { label: "The test writer reads the code", role: "ppl-test-worker", tool: "Bash", value: "cat src/main/Foo.java" },
    { label: "Anyone stages every file", role: "operator", tool: "Bash", value: "git add -A" },
    { label: "Anyone force-pushes", role: "operator", tool: "Bash", value: "git push --force origin main" },
  ];
}

export function PlaygroundPage() {
  const [active, setActive] = useState(null);
  const [role, setRole] = useState("ppl-code-worker");
  const [tool, setTool] = useState("Bash");
  const [value, setValue] = useState("");
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => {
    let live = true;
    get("/api/harnesses")
      .then((list) => (list.active ? get("/api/harnesses/" + encodeURIComponent(list.active)) : false))
      .then((found) => { if (live) { setActive(found); } }, (e) => { if (live) { setError(e.message); } });
    return () => { live = false; };
  }, []);
  if (error && active === null) {
    return html`<${Notice} kind="error">${error}<//>`;
  }
  if (active === null) {
    return html`<p class="muted">Loading...</p>`;
  }
  if (active === false) {
    return html`<${Notice} kind="warn">The playground checks a call against the active harness. Finish setup first.<//>`;
  }
  const run = async (r, t, v) => {
    if (busy) {
      return;
    }
    setRole(r);
    setTool(t);
    setValue(v);
    setBusy(true);
    setError(null);
    try {
      const answer = await post("/api/playground", { name: active.name, role: r, tool: t, value: v });
      setResult(Object.assign({ asked: { role: r, tool: t, value: v } }, answer));
    } catch (e) {
      setResult(null);
      setError(e.message);
    }
    setBusy(false);
  };
  const quiet = result && (result.decision === "allow" ? "The cage lets this call through." : "The hook gave no reason.");
  return html`<section>
    <h1>Cage playground</h1>
    <p class="muted">Try one tool call against the ${active.name} harness. The real guard hook answers, on a throwaway run: nothing is written.</p>
    <div>${examples(active.doc).map((ex) => html`<button disabled=${busy} onClick=${() => run(ex.role, ex.tool, ex.value)}>${ex.label}</button>`)}</div>
    <div class="grid">
      <${Field} label="Who calls"><select value=${role} onChange=${(e) => setRole(e.target.value)}>
        ${ROLES.map((r) => html`<option value=${r}>${r === "operator" ? "you (no role)" : r}</option>`)}</select><//>
      <${Field} label="Tool"><select value=${tool} onChange=${(e) => setTool(e.target.value)}>
        ${TOOLS.map((t) => html`<option value=${t}>${t}</option>`)}</select><//>
    </div>
    <${Field} label=${tool === "Bash" ? "Command" : "Path"} help="A relative path is read from the project checkout.">
      <input value=${value} onInput=${(e) => setValue(e.target.value)}
        onKeyDown=${(e) => { if (e.key === "Enter" && value.trim()) { run(role, tool, value); } }} /><//>
    <${Button} primary busy=${busy} disabled=${!value.trim()} onClick=${() => run(role, tool, value)}>Check<//>
    ${error ? html`<${Notice} kind="error">${error}<//>` : null}
    ${result ? html`<div class="card">
      <p class="muted"><code>${result.asked.tool}</code> by ${result.asked.role === "operator" ? "you" : result.asked.role}: <code>${result.asked.value}</code></p>
      <${Badge} kind=${result.decision === "allow" ? "done" : "flag"}>${result.decision}<//>
      ${result.reason ? html`<p>${result.reason}</p>` : html`<p class="muted">${quiet}</p>`}
      ${result.rules && result.rules.length ? html`<p class="help">Rules: ${result.rules.join(", ")}</p>` : null}
    </div>` : null}
  </section>`;
}
