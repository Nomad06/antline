import { html, useEffect, useRef, useState } from "../vendor/htm-preact.mjs";
import { post } from "../api.js";
import { Badge, Button, Field, Notice } from "./controls.js";

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function words(text) {
  return text.split(",").map((part) => part.trim()).filter(Boolean);
}

export function HarnessForm({ draft, extras, mode, saveLabel, onSave }) {
  const [name, setName] = useState(draft.doc.harness.name);
  const [doc, setDoc] = useState(clone(draft.doc));
  const [fragments, setFragments] = useState(clone(draft.fragments || {}));
  const [check, setCheck] = useState(null);
  const [pending, setPending] = useState(true);
  const [acked, setAcked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [replaceable, setReplaceable] = useState(false);
  const timer = useRef(null);
  const sources = draft.sources || {};
  const notes = (extras && extras.notes) || [];
  const dropped = (extras && extras.dropped) || [];

  useEffect(() => {
    let current = true;
    const land = (result) => {
      if (current) {
        setCheck(result);
        setPending(false);
      }
    };
    setPending(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      post("/api/harness/draft/check", { name, doc, fragments }).then(land,
        (e) => land({ ok: false, errors: [e.message], warnings: [], flags: [] }));
    }, 400);
    return () => {
      current = false;
      clearTimeout(timer.current);
    };
  }, [name, doc, fragments]);

  const update = (mutate) => {
    const next = clone(doc);
    mutate(next);
    setDoc(next);
    setAcked(false);
  };
  const gates = doc.gate || [];
  const shapes = (doc.guard && doc.guard.shape) || [];
  const env = Object.entries(doc.env || {});
  const flagged = Boolean(check && check.flags && check.flags.length);
  const flagsFor = (index) => (check && check.flags ? check.flags.filter((f) => f.shape === index) : []);
  const save = async (replace) => {
    setBusy(true);
    setError(null);
    try {
      await onSave({ name, doc, fragments, replace });
      setReplaceable(false);
    } catch (e) {
      setError(e.message);
      setReplaceable(/already installed/.test(e.message));
    }
    setBusy(false);
  };

  return html`<div class="card">
    <h3>Review the harness</h3>
    ${notes.map((note) => html`<${Notice} kind="warn">${note}<//>`)}
    ${dropped.length ? html`<details><summary>${dropped.length} item(s) left out</summary>
      <ul>${dropped.map((item) => html`<li><code>${item.item}</code>: ${item.reason}</li>`)}</ul></details>` : null}
    <div class="grid">
      <${Field} label="Harness name"><input value=${name} onInput=${(e) => setName(e.target.value)} /><//>
      <${Field} label="Default branch" help=${sources["repo.default_branch"] || ""}>
        <input value=${doc.repo.default_branch} onInput=${(e) => update((d) => { d.repo.default_branch = e.target.value; })} /><//>
      <${Field} label="Task key pattern" help="A regex every task key matches, for example ^APP-[0-9]+$">
        <input value=${doc.task.key_pattern} onInput=${(e) => update((d) => { d.task.key_pattern = e.target.value; })} /><//>
      <${Field} label="Task URL" help="<TASK-ID> stands for the key">
        <input value=${doc.task.url_template} onInput=${(e) => update((d) => { d.task.url_template = e.target.value; })} /><//>
      <${Field} label="Forge" help=${sources["forge.kind"] || ""}>
        <select value=${doc.forge.kind} onChange=${(e) => update((d) => {
          d.forge = { kind: e.target.value };
          if (e.target.value === "gitlab") { d.forge.mcp_server = "GitLab-MCP"; }
        })}>
          <option value="github">GitHub</option>
          <option value="gitlab">GitLab</option>
        </select><//>
      ${doc.forge.kind === "gitlab" ? html`<${Field} label="GitLab MCP server name">
        <input value=${doc.forge.mcp_server || ""} onInput=${(e) => update((d) => { d.forge.mcp_server = e.target.value; })} /><//>` : null}
    </div>

    <h3>Build gates</h3>
    <p class="help">The commands the orchestrator runs and records. One must be named <code>focused-tests</code>: it runs one test, and <code>${"<test_class>"}</code> stands for that test's name.</p>
    <div class="rows">${gates.map((gate, i) => html`<div class="row">
      <input value=${gate.name} aria-label="gate name" onInput=${(e) => update((d) => { d.gate[i].name = e.target.value; })} />
      <input value=${gate.command} aria-label="gate command" onInput=${(e) => update((d) => { d.gate[i].command = e.target.value; })} />
      <${Badge}>${sources["gate:" + gate.name] || "edited"}<//>
      <button onClick=${() => update((d) => { d.gate.splice(i, 1); })}>Remove</button>
    </div>`)}</div>
    <button onClick=${() => update((d) => { d.gate = (d.gate || []).concat([{ name: "", command: "" }]); })}>Add a gate</button>

    <h3>Commands the test writer may run</h3>
    <p class="help">The test writer does not see the code it tests, so its shell is held to these build commands.</p>
    ${shapes.map((shape, i) => html`<div class="card">
      <div class="grid">
        <${Field} label="Program"><input value=${shape.argv0} onInput=${(e) => update((d) => { d.guard.shape[i].argv0 = e.target.value; })} /><//>
        <${Field} label="Allowed goals" help="Comma separated, exactly one per command line">
          <input value=${(shape.one_goal_of || []).join(", ")} onInput=${(e) => update((d) => { d.guard.shape[i].one_goal_of = words(e.target.value); })} /><//>
        ${mode === "expert" ? html`<${Field} label="Other arguments (a regex each token must match)">
          <input value=${shape.free_args || ""} onInput=${(e) => update((d) => { d.guard.shape[i].free_args = e.target.value; })} /><//>` : null}
      </div>
      <${Badge}>${sources["shape:" + i] || "edited"}<//>
      ${flagsFor(i).map((flag) => flag.reasons.map((reason) => html`<p><${Badge} kind="flag">check<//> ${reason}</p>`))}
    </div>`)}

    <h3>Build environment</h3>
    <div class="rows">${env.map(([key, value]) => html`<div class="row">
      <input value=${key} aria-label="variable" onInput=${(e) => update((d) => { d.env = Object.fromEntries(Object.entries(d.env).map(([k, v]) => (k === key ? [e.target.value, v] : [k, v]))); })} />
      <input value=${value} aria-label="value" onInput=${(e) => update((d) => { d.env[key] = e.target.value; })} />
      <${Badge}>${sources["env:" + key] || "edited"}<//>
      <button onClick=${() => update((d) => { delete d.env[key]; })}>Remove</button>
    </div>`)}</div>
    <button onClick=${() => update((d) => { d.env = Object.assign({}, d.env || {}, { NEW_VARIABLE: "" }); })}>Add a variable</button>

    ${mode === "expert" ? html`<h3>Fragments</h3>
      ${Object.keys(fragments).sort().map((rel) => html`<${Field} label=${rel}>
        <textarea value=${fragments[rel]} onInput=${(e) => setFragments(Object.assign({}, fragments, { [rel]: e.target.value }))}></textarea><//>`)}` : null}

    ${check && check.errors.length ? html`<${Notice} kind="error">${check.errors.join(" ")}<//>` : null}
    ${check && check.warnings.length ? html`<${Notice} kind="warn">${check.warnings.join(" ")}<//>` : null}
    ${flagged ? html`<label class="check"><input type="checkbox" checked=${acked} onChange=${(e) => setAcked(e.target.checked)} />
      I checked the commands marked check, install them as they are</label>` : null}
    <${Button} primary busy=${busy} disabled=${pending || !check || !check.ok || (flagged && !acked)} onClick=${() => save(false)}>${saveLabel || "Install the harness"}<//>
    ${replaceable ? html`<${Button} busy=${busy} onClick=${() => save(true)}>Replace the installed harness<//>` : null}
    ${error ? html`<${Notice} kind="error">${error}<//>` : null}
  </div>`;
}
