import { html, useEffect, useRef, useState } from "../vendor/htm-preact.mjs";
import { post } from "../api.js";
import { Badge, Button, Field, Notice } from "./controls.js";

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function words(text) {
  return text.split(",").map((part) => part.trim()).filter(Boolean);
}

function withEnv(doc, rows) {
  const env = {};
  rows.forEach(([key, value]) => {
    if (key.trim()) {
      env[key] = value;
    }
  });
  return Object.assign({}, doc, { env });
}

function rowsProblem(rows) {
  if (rows.some(([key]) => !key.trim())) {
    return "Every variable needs a name: fill it in or remove the row.";
  }
  const seen = new Set();
  for (const [key] of rows) {
    if (seen.has(key)) {
      return key + " is set twice: keep one row.";
    }
    seen.add(key);
  }
  return null;
}

export function HarnessForm({ draft, extras, mode, saveLabel, onSave, nameLocked }) {
  const [name, setName] = useState(draft.doc.harness.name);
  const [doc, setDoc] = useState(() => {
    const copy = clone(draft.doc);
    delete copy.env;
    return copy;
  });
  const [envRows, setEnvRows] = useState(() => Object.entries(draft.doc.env || {}));
  const [fragments, setFragments] = useState(clone(draft.fragments || {}));
  const [check, setCheck] = useState(null);
  const [acked, setAcked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [replaceable, setReplaceable] = useState(false);
  const live = useRef(null);
  live.current = { name, doc, envRows, fragments };
  const sources = draft.sources || {};
  const notes = (extras && extras.notes) || [];
  const dropped = (extras && extras.dropped) || [];

  useEffect(() => {
    let current = true;
    const inputs = { name, doc, envRows, fragments };
    const land = (result) => {
      if (current) {
        setCheck({ for: inputs, result });
      }
    };
    const timer = setTimeout(() => {
      post("/api/harness/draft/check", { name, doc: withEnv(doc, envRows), fragments }).then(land,
        (e) => land({ ok: false, errors: [e.message], warnings: [], flags: [] }));
    }, 400);
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [name, doc, envRows, fragments]);

  const touch = () => {
    setReplaceable(false);
    setError(null);
  };
  const update = (mutate) => {
    const next = clone(doc);
    mutate(next);
    setDoc(next);
    setAcked(false);
    touch();
  };
  const editRows = (change) => {
    setEnvRows(change(envRows));
    touch();
  };
  const gates = doc.gate || [];
  const shapes = (doc.guard && doc.guard.shape) || [];
  const result = check && check.result;
  const fresh = Boolean(check) && check.for.name === name && check.for.doc === doc
    && check.for.envRows === envRows && check.for.fragments === fragments;
  const flagged = Boolean(result && result.flags && result.flags.length);
  const flagsFor = (index) => (result && result.flags ? result.flags.filter((f) => f.shape === index) : []);
  const problem = rowsProblem(envRows);
  const canSave = fresh && result.ok && !problem && (!flagged || acked);
  const save = async (replace) => {
    if (!canSave) {
      return;
    }
    const started = live.current;
    setBusy(true);
    setError(null);
    try {
      await onSave({ name, doc: withEnv(doc, envRows), fragments, replace });
      setReplaceable(false);
    } catch (e) {
      const now = live.current;
      const unchanged = now.name === started.name && now.doc === started.doc
        && now.envRows === started.envRows && now.fragments === started.fragments;
      setError(e.message);
      setReplaceable(unchanged && /already installed/.test(e.message));
    }
    setBusy(false);
  };

  return html`<div class="card">
    <h3>Review the harness</h3>
    ${notes.map((note) => html`<${Notice} kind="warn">${note}<//>`)}
    ${dropped.length ? html`<details><summary>${dropped.length} item(s) left out</summary>
      <ul>${dropped.map((item) => html`<li><code>${item.item}</code>: ${item.reason}</li>`)}</ul></details>` : null}
    <div class="grid">
      <${Field} label="Harness name"><input value=${name} disabled=${nameLocked} onInput=${(e) => { setName(e.target.value); touch(); }} /><//>
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
    <div class="rows">${envRows.map(([key, value], i) => html`<div class="row">
      <input value=${key} aria-label="variable" onInput=${(e) => editRows((rows) => rows.map((row, at) => (at === i ? [e.target.value, row[1]] : row)))} />
      <input value=${value} aria-label="value" onInput=${(e) => editRows((rows) => rows.map((row, at) => (at === i ? [row[0], e.target.value] : row)))} />
      <${Badge}>${sources["env:" + key] || "edited"}<//>
      <button onClick=${() => editRows((rows) => rows.filter((row, at) => at !== i))}>Remove</button>
    </div>`)}</div>
    ${problem ? html`<${Notice} kind="error">${problem}<//>` : null}
    <button onClick=${() => editRows((rows) => rows.concat([["", ""]]))}>Add a variable</button>

    ${mode === "expert" ? html`<h3>Fragments</h3>
      ${Object.keys(fragments).sort().map((rel) => html`<${Field} label=${rel}>
        <textarea value=${fragments[rel]} onInput=${(e) => { setFragments(Object.assign({}, fragments, { [rel]: e.target.value })); touch(); }}></textarea><//>`)}` : null}

    ${result && result.errors.length ? html`<${Notice} kind="error">${result.errors.join(" ")}<//>` : null}
    ${result && result.warnings.length ? html`<${Notice} kind="warn">${result.warnings.join(" ")}<//>` : null}
    ${flagged ? html`<label class="check"><input type="checkbox" checked=${acked} onChange=${(e) => setAcked(e.target.checked)} />
      I checked the commands marked check, install them as they are</label>` : null}
    <${Button} primary busy=${busy} disabled=${!canSave} onClick=${() => save(false)}>${saveLabel || "Install the harness"}<//>
    ${replaceable ? html`<${Button} busy=${busy} disabled=${!canSave} onClick=${() => save(true)}>Replace the installed harness<//>` : null}
    ${error ? html`<${Notice} kind="error">${error}<//>` : null}
  </div>`;
}
