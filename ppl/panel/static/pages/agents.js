import { html, useEffect, useState } from "../vendor/htm-preact.mjs";
import { get, post, put, runJob } from "../api.js";
import { Badge, Button, Field, Log, Notice } from "../ui/controls.js";
import { History } from "../ui/history.js";

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function RoleDetail({ role, routingDoc, mode, onDirty }) {
  const [detail, setDetail] = useState(null);
  const [notes, setNotes] = useState("");
  const [rendered, setRendered] = useState("");
  const [error, setError] = useState(null);
  const [problem, setProblem] = useState(null);
  const [previewProblem, setPreviewProblem] = useState(null);
  const [saved, setSaved] = useState(null);
  const [busy, setBusy] = useState(false);
  const ready = Boolean(detail && detail.harness);
  const unsaved = Boolean(detail && detail.overlay && notes !== detail.overlay.text);
  const show = (data) => {
    setDetail(data);
    setNotes(data.overlay ? data.overlay.text : "");
    setRendered(data.rendered || "");
  };
  const reload = () => get("/api/agents/" + encodeURIComponent(role)).then((data) => {
    show(data);
    setSaved(null);
    setProblem(null);
  }, (e) => setProblem(e.message));
  useEffect(() => {
    get("/api/agents/" + encodeURIComponent(role)).then(show, (e) => setError(e.message));
  }, [role]);
  useEffect(() => {
    onDirty(unsaved);
    return () => onDirty(false);
  }, [unsaved]);
  useEffect(() => {
    if (!ready) {
      return undefined;
    }
    let live = true;
    const timer = setTimeout(() => {
      post("/api/agents/" + encodeURIComponent(role) + "/preview", { overlay: notes, routing: routingDoc }).then(
        (r) => { if (live) { setRendered(r.rendered); setPreviewProblem(null); } },
        (e) => { if (live) { setPreviewProblem(e.message); } });
    }, 400);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [notes, routingDoc, ready]);
  if (error) {
    return html`<${Notice} kind="error">${error}<//>`;
  }
  if (!detail) {
    return html`<p class="muted">Loading...</p>`;
  }
  const edit = (text) => {
    setNotes(text);
    setSaved(null);
    setProblem(null);
  };
  const save = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const r = await put("/api/agents/" + encodeURIComponent(role) + "/overlay", { text: notes, hash: detail.overlay.hash });
      setDetail(Object.assign({}, detail, { overlay: { key: detail.overlay.key, text: notes, hash: r.hash } }));
      setSaved("Saved. Apply to write the agent file.");
    } catch (e) {
      setProblem(e.message);
    }
    setBusy(false);
  };
  return html`<div>
    <div class="panes">
      <div><h3>Base prompt</h3><p class="help">Part of antline, the same for every project.</p><pre>${detail.base}</pre></div>
      <div><h3>Project notes for this role</h3>
        ${detail.harness ? html`<textarea aria-label="Project notes for this role" value=${notes} onInput=${(e) => edit(e.target.value)}></textarea>
          <${Button} primary busy=${busy} disabled=${!unsaved} onClick=${save}>Save the notes<//>
          ${saved ? html`<${Notice} kind="ok">${saved}<//>` : null}
          ${problem ? html`<${Notice} kind="error">${problem}<//>` : null}`
          : html`<p class="muted">Set up a harness first: the notes live in it.</p>`}
      </div>
      <div><h3>The agent file apply writes</h3>
        ${previewProblem ? html`<${Notice} kind="error">${previewProblem}<//>` : null}
        <pre>${rendered || "No harness yet."}</pre></div>
    </div>
    <details><summary>What the cage enforces for this role, whatever the prompt says (${detail.enforced.length})</summary>
      <ul>${detail.enforced.map((rule) => html`<li><code>${rule.id}</code> ${rule.statement}
        ${mode === "expert" && rule.residual ? html`<br /><span class="muted">Not covered: ${rule.residual}</span>` : null}</li>`)}</ul>
    </details>
    ${mode === "expert" && detail.harness ? html`<h3>History of the notes</h3>
      <${History} fileKey=${"harness:" + detail.harness + ":templates/" + role + ".part"} stamp=${detail.overlay.hash} onRestored=${reload} />` : null}
  </div>`;
}

export function AgentsPage({ mode }) {
  const [data, setData] = useState(null);
  const [doc, setDoc] = useState(null);
  const [open, setOpen] = useState(null);
  const [notesDirty, setNotesDirty] = useState(false);
  const [error, setError] = useState(null);
  const [problem, setProblem] = useState(null);
  const [message, setMessage] = useState(null);
  const [busy, setBusy] = useState(null);
  const [log, setLog] = useState([]);
  const load = () => get("/api/agents").then((d) => {
    setError(null);
    setData(d);
    setDoc(clone(d.routing));
  }, (e) => setError(e.message));
  useEffect(() => { load(); }, []);
  if (error) {
    // routing.toml itself does not load: say so, and offer the way back to a version that did
    return html`<section><${Notice} kind="error">${error}<//>
      ${mode === "expert" ? html`<h3>History of routing.toml</h3><${History} fileKey="routing" onRestored=${load} />`
        : html`<p class="muted">Fix routing.toml in an editor, or switch to Expert mode to restore an earlier version.</p>`}</section>`;
  }
  if (!data || !doc) {
    return html`<p class="muted">Loading...</p>`;
  }
  const setRole = (role, mutate) => {
    const next = clone(doc);
    mutate(next.roles[role]);
    setDoc(next);
  };
  const dirty = JSON.stringify(doc) !== JSON.stringify(data.routing);
  const tiers = Object.keys(data.tiers).sort();
  const failed = (e) => { setMessage(null); setProblem(e.message); };
  const done = (text) => { setProblem(null); setMessage(text); };
  const act = async (kind, work) => {
    setBusy(kind);
    try {
      await work();
    } catch (e) {
      failed(e);
    }
    setBusy(null);
  };
  const save = () => act("save", async () => {
    await put("/api/routing", { config: doc, hash: data.routing_hash });
    done("Saved. Apply to write the agent files.");
    await load();
  });
  const preset = (name) => act("preset", async () => {
    await post("/api/routing/preset", { name });
    done("The " + name + " preset is in routing.toml. Apply to write the agent files.");
    await load();
  });
  const apply = () => act("apply", async () => {
    setLog([]);
    await runJob(post("/api/harnesses/" + encodeURIComponent(data.harness) + "/apply"), (lines) => setLog((old) => old.concat(lines)));
    done("The agent files and the skill are written.");
  });
  const applyHelp = dirty ? "Save the routing first: apply writes the saved one."
    : notesDirty ? "Save the project notes first: apply writes the saved ones."
    : data.harness ? "Apply writes the six agent files and the skill from the saved routing and the active harness."
      : data.harness_error ? "Apply is off until the active harness loads."
        : "Apply needs an active harness: set one up on the Harness page.";
  const setByRisk = (entry, tier) => {
    const next = Object.assign({}, entry.by_risk);
    if (tier) {
      next.high = tier;
    } else {
      delete next.high;
    }
    if (Object.keys(next).length) {
      entry.by_risk = next;
    } else {
      delete entry.by_risk;
    }
  };
  const setEffort = (entry, level) => {
    const params = Object.assign({}, entry.params);
    if (level) {
      params.effort = level;
    } else {
      delete params.effort;
    }
    if (Object.keys(params).length) {
      entry.params = params;
    } else {
      delete entry.params;
    }
  };
  return html`<section>
    <h1>Agents</h1>
    <p class="muted">Each role runs as its own Claude Code subagent. Changes reach the agent files when you apply, and a run already started keeps the files it started with.</p>
    ${data.harness_error ? html`<${Notice} kind="warn">The active harness does not load, so Apply is off. Fix it on the Harness page. The routing can still be edited here.<br />${data.harness_error}<//>` : null}
    <div class="cards">${data.presets.map((p) => html`<button class=${"card" + (data.preset === p.name ? " on" : "")} disabled=${Boolean(busy)} onClick=${() => preset(p.name)}>
      <strong>${p.label}</strong> <${Badge}>${p.cost} cost<//></button>`)}</div>
    <p class="help">A preset sets the tier and the effort of every role and writes routing.toml at once. Comments in it are dropped and no earlier version is kept.</p>
    ${data.roles.map((row) => {
      const entry = doc.roles[row.role];
      const params = entry.params || {};
      const shown = open === row.role;
      const toggle = () => setOpen(shown ? null : row.role);
      return html`<article class="step">
        <header role="button" tabIndex="0" aria-expanded=${shown} onClick=${toggle}
          onKeyDown=${(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } }}>
          <h2>${row.role.replace("ppl-", "")}</h2>
          <${Badge} kind=${row.verified === "verified" ? "done" : "skipped"}>${row.model}: ${row.verified}<//>
        </header>
        <p class="muted">${row.description}</p>
        <div class="grid">
          <${Field} label="Model tier"><select value=${entry.tier || ""} onChange=${(e) => setRole(row.role, (r) => { r.tier = e.target.value; delete r.provider; delete r.model; })}>
            ${entry.tier ? null : html`<option value="">${row.model} (named in routing.toml)</option>`}
            ${tiers.map((tier) => html`<option value=${tier}>${tier} (${data.tiers[tier].model})</option>`)}</select><//>
          <${Field} label="Effort"><select value=${params.effort || ""} onChange=${(e) => setRole(row.role, (r) => setEffort(r, e.target.value))}>
            <option value="">not set</option>
            ${data.effort_levels.map((level) => html`<option value=${level}>${level}</option>`)}</select><//>
          ${mode === "expert" ? html`<${Field} label="Tier for high-risk runs" help="Runs started with ppl init --risk high">
            <select value=${(entry.by_risk || {}).high || ""} onChange=${(e) => setRole(row.role, (r) => setByRisk(r, e.target.value))}>
              <option value="">the same tier</option>
              ${tiers.map((tier) => html`<option value=${tier}>${tier}</option>`)}</select><//>` : null}
        </div>
        <p class="help">Tools: ${row.tools.join(", ")}</p>
        ${shown ? html`<${RoleDetail} key=${row.role} role=${row.role} routingDoc=${doc} mode=${mode} onDirty=${setNotesDirty} />` : null}
      </article>`;
    })}
    <${Button} primary busy=${busy === "save"} disabled=${!dirty || Boolean(busy)} onClick=${save}>Save the routing<//>
    <p class="help">Saving writes routing.toml from this form, so comments in it are dropped. Its history keeps the old file.</p>
    <${Button} busy=${busy === "apply"} disabled=${!data.harness || dirty || notesDirty || Boolean(busy)} onClick=${apply}>Apply<//>
    <p class="help">${applyHelp}</p>
    ${message ? html`<${Notice} kind="ok">${message}<//>` : null}
    ${problem ? html`<${Notice} kind="error">${problem}<//>` : null}
    <${Log} lines=${log} />
    ${mode === "expert" ? html`<h3>History of routing.toml</h3><${History} fileKey="routing" stamp=${data.routing_hash} onRestored=${load} />` : null}
  </section>`;
}
