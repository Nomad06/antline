import { html, useEffect, useState } from "../vendor/htm-preact.mjs";
import { get, post } from "../api.js";
import { Badge, Button, Field, Log, Measured, Notice } from "../ui/controls.js";
import { useStepRunner } from "../ui/step.js";
import { HarnessStep } from "./setup-harness.js";
import { AntMark } from "../ui/ant.js";

const TITLES = { paths: "Welcome", claude: "Claude Code", models: "Models",
                 harness: "Project and harness", forge: "Forge", apply: "Apply" };

function Outcome({ step }) {
  return html`<${Log} lines=${step.log} />${step.error ? html`<${Notice} kind="error">${step.error}<//>` : null}`;
}

function PathsStep({ state, done }) {
  const step = useStepRunner("paths", done);
  return html`<div>
    <p>antline keeps its settings, verified models and harnesses in <code>${state.data_root}</code>. The assets directory is <code>${state.code_root || "unknown"}</code>.</p>
    <${Button} primary busy=${step.busy} onClick=${() => step.run({})}>Create the data root<//>
    <${Outcome} step=${step} />
  </div>`;
}

function ClaudeStep({ done }) {
  const step = useStepRunner("claude", done);
  const [binary, setBinary] = useState("claude");
  return html`<div>
    <${Field} label="Claude Code binary" help="ppl lane starts each task's Claude Code window with it.">
      <input value=${binary} onInput=${(e) => setBinary(e.target.value)} /><//>
    <p class="help">A workspace an older antline set up is cleaned here: its hook entries, sandbox block, pipeline skill and ppl agents go, anything else in it stays.</p>
    <${Button} primary busy=${step.busy} disabled=${!binary.trim()} onClick=${() => step.run({ claude_binary: binary })}>Save<//>
    <${Outcome} step=${step} />
  </div>`;
}

function ModelsStep({ mode, done }) {
  const step = useStepRunner("models", done);
  const [info, setInfo] = useState(null);
  const [tiers, setTiers] = useState({});
  const [error, setError] = useState(null);
  const show = (data) => {
    setError(null);
    setInfo(data);
    setTiers(Object.fromEntries(Object.entries(data.tiers).map(([tier, entry]) => [tier, entry.model])));
  };
  useEffect(() => {
    get("/api/routing/presets").then(show, (e) => setError(e.message));
  }, []);
  if (!info) {
    return error ? html`<${Notice} kind="error">${error}<//>` : html`<p class="muted">Loading...</p>`;
  }
  const choose = (name) => {
    setError(null);
    return post("/api/routing/preset", { name, hash: info.routing_hash }).then(show, (e) => setError(e.message));
  };
  const answers = { verify: "yes" };
  Object.entries(tiers).forEach(([tier, model]) => { answers["tier." + tier] = model; });
  return html`<div>
    <div class="cards">${info.presets.map((preset) => html`
      <button class=${"card" + (info.current === preset.name ? " on" : "")} onClick=${() => choose(preset.name)}>
        <strong>${preset.label}</strong> <${Badge}>${preset.cost} cost<//>
        <ul>${preset.roles.map((role) => html`<li>${role.role.replace("ppl-", "")}: ${role.tier}, ${role.effort}</li>`)}</ul>
        <${Measured} stat=${(info.measured || {})[preset.name]} />
      </button>`)}</div>
    ${error ? html`<${Notice} kind="error">${error}<//>` : null}
    ${mode === "expert" ? html`<div class="grid">${Object.keys(tiers).sort().map((tier) => html`
      <${Field} label=${"Model for tier " + tier}><input value=${tiers[tier]} onInput=${(e) => setTiers(Object.assign({}, tiers, { [tier]: e.target.value }))} /><//>`)}</div>` : null}
    <${Button} primary busy=${step.busy} onClick=${() => step.run(answers)}>Verify the models<//>
    <p class="help">One short probe per model, about a cent in all. A run cannot start until every model it uses answers.</p>
    <${Outcome} step=${step} />
  </div>`;
}

function ForgeStep({ done }) {
  const step = useStepRunner("forge", done);
  return html`<div>
    <p>Checks that ppl can reach the forge the harness names: <code>gh auth status</code> for GitHub, the MCP server registration for GitLab.</p>
    <${Button} primary busy=${step.busy} onClick=${() => step.run({})}>${step.error ? "Check again" : "Check the forge"}<//>
    <${Outcome} step=${step} />
  </div>`;
}

function binaryFound(doctor) {
  const found = doctor.claude_binary || {};
  return found.path ? "yes, " + found.path : "no: " + (found.binary || "claude") + " is not on PATH";
}

function DoctorCard({ doctor }) {
  return html`<div class="card">
    <h3>Ready check</h3>
    <ul>
      <li>Claude Code binary found: ${binaryFound(doctor)}</li>
      <li>Default harness: ${(doctor.harness && doctor.harness.active) || "none"}</li>
    </ul>
    <h3>Start your first task</h3>
    <p>Run this in a terminal with the task's tracker link or key. It opens a Claude Code window for that task alone, with its own worktree and its guard:</p>
    <pre>ppl lane KEY-123</pre>
  </div>`;
}

function ApplyStep({ done }) {
  const [doctor, setDoctor] = useState(null);
  const step = useStepRunner("apply", async () => {
    await done();
    setDoctor(await get("/api/doctor"));
  });
  return html`<div>
    <p>Checks the harness at its commit and makes it the default for <code>ppl lane</code>. Commit any change to the harness first: a lane runs only what is committed.</p>
    <${Button} primary busy=${step.busy} onClick=${() => step.run({})}>Apply<//>
    <${Outcome} step=${step} />
    ${doctor ? html`<${DoctorCard} doctor=${doctor} />` : null}
  </div>`;
}

const VIEWS = { paths: PathsStep, claude: ClaudeStep, models: ModelsStep, harness: HarnessStep,
                forge: ForgeStep, apply: ApplyStep };

export function SetupPage({ mode }) {
  const [state, setState] = useState(null);
  const [open, setOpen] = useState(null);
  const [error, setError] = useState(null);
  const [problem, setProblem] = useState(null);
  const refresh = async () => {
    const fresh = await get("/api/setup");
    setState(fresh);
    setOpen(fresh.next || "apply");
    return fresh;
  };
  useEffect(() => {
    refresh().catch((e) => setError(e.message));
  }, []);
  if (error) {
    return html`<${Notice} kind="error">${error}<//>`;
  }
  if (!state) {
    return html`<p class="muted">Loading...</p>`;
  }
  const skip = (name) => {
    setProblem(null);
    return post("/api/setup/" + name + "/skip").then(refresh)
      .catch((e) => setProblem({ step: name, message: e.message }));
  };
  return html`<section>
    <h1>Set up antline</h1>
    <p class="muted">Every step can be skipped and run again later. <code>ppl setup</code> in a terminal reads and writes the same state.</p>
    <div class="trail">${state.steps.map((item) => {
      const View = VIEWS[item.name];
      return html`<article class=${"step " + item.state + (open === item.name ? " open" : "")}>
        ${open === item.name ? html`<span class="here"><${AntMark} /></span>` : null}
        <header onClick=${() => setOpen(item.name)}>
          <h2>${TITLES[item.name]}</h2> <${Badge} kind=${item.state}>${item.state}<//>
        </header>
        ${open === item.name ? html`<div class="body">
          <p class="muted">${item.intro}</p>
          <${View} state=${state} mode=${mode} done=${refresh} />
          ${problem && problem.step === item.name ? html`<${Notice} kind="error">${problem.message}<//>` : null}
          <p class="skip"><a href="#/setup" onClick=${(e) => { e.preventDefault(); skip(item.name); }}>Skip, set this up later</a></p>
        </div>` : null}
      </article>`;
    })}</div>
  </section>`;
}
