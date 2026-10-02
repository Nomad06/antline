import { html, useEffect, useState } from "../vendor/htm-preact.mjs";
import { get, post } from "../api.js";
import { Badge, Button, Field, Log, Notice } from "../ui/controls.js";
import { useStepRunner } from "../ui/step.js";
import { HarnessStep } from "./setup-harness.js";

const TITLES = { paths: "Welcome", claude: "Claude Code", models: "Models",
                 harness: "Project and harness", forge: "Forge", apply: "Apply" };

function Outcome({ step }) {
  return html`<${Log} lines=${step.log} />${step.error ? html`<${Notice} kind="error">${step.error}<//>` : null}`;
}

function PathsStep({ state, done }) {
  const step = useStepRunner("paths", done);
  return html`<div>
    <p>antline keeps its settings, verified models and harnesses in <code>${state.data_root}</code>. The code is in <code>${state.code_root || "unknown"}</code>.</p>
    <${Button} primary busy=${step.busy} onClick=${() => step.run({})}>Create the data root<//>
    <${Outcome} step=${step} />
  </div>`;
}

function ClaudeStep({ state, mode, done }) {
  const step = useStepRunner("claude", done);
  const [binary, setBinary] = useState("claude");
  const [workspace, setWorkspace] = useState(state.workspace || "~/ppl-workspace");
  const [hooks, setHooks] = useState("local");
  const choices = mode === "expert" ? ["local", "global", "manual"] : ["local", "global"];
  const labels = { local: "Only in this workspace (recommended)", global: "In all my Claude Code projects",
                   manual: "Print the entries, I paste them myself" };
  return html`<div>
    <${Field} label="Workspace folder" help="Pipeline sessions start here. Its .claude folder gets the agents, the skill and the hooks.">
      <input value=${workspace} onInput=${(e) => setWorkspace(e.target.value)} /><//>
    ${mode === "expert" ? html`<${Field} label="Claude Code binary"><input value=${binary} onInput=${(e) => setBinary(e.target.value)} /><//>` : null}
    <fieldset><legend>Where the guard hooks go</legend>
      ${choices.map((choice) => html`<label><input type="radio" name="hooks" checked=${hooks === choice} onChange=${() => setHooks(choice)} />${labels[choice]}</label>`)}
    </fieldset>
    <${Button} primary busy=${step.busy} disabled=${!workspace.trim()} onClick=${() => step.run({ claude_binary: binary, workspace, hooks })}>Save and register the hooks<//>
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
    return post("/api/routing/preset", { name }).then(show, (e) => setError(e.message));
  };
  const answers = { verify: "yes" };
  Object.entries(tiers).forEach(([tier, model]) => { answers["tier." + tier] = model; });
  return html`<div>
    <div class="cards">${info.presets.map((preset) => html`
      <button class=${"card" + (info.current === preset.name ? " on" : "")} onClick=${() => choose(preset.name)}>
        <strong>${preset.label}</strong> <${Badge}>${preset.cost} cost<//>
        <ul>${preset.roles.map((role) => html`<li>${role.role.replace("ppl-", "")}: ${role.tier}, ${role.effort}</li>`)}</ul>
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

function DoctorCard({ doctor, workspace }) {
  return html`<div class="card">
    <h3>Ready check</h3>
    <ul>
      <li>Guard hook runs: ${doctor.hook_runs === true ? "yes" : "no"}${doctor.hook_runs_problem ? ": " + doctor.hook_runs_problem : ""}</li>
      <li>Hook registration: ${doctor.registration}</li>
      <li>Active harness: ${(doctor.harness && doctor.harness.active) || "none"}</li>
    </ul>
    <h3>Start your first task</h3>
    <p>Open Claude Code in <code>${workspace || "your workspace"}</code> and type the task's key after the skill name, a tracker link works too:</p>
    <pre>/pipeline KEY-123</pre>
  </div>`;
}

function ApplyStep({ state, done }) {
  const [doctor, setDoctor] = useState(null);
  const step = useStepRunner("apply", async () => {
    await done();
    setDoctor(await get("/api/doctor"));
  });
  return html`<div>
    <p>Writes the six agent files and the orchestrator skill into the workspace and makes the harness active.</p>
    <${Button} primary busy=${step.busy} onClick=${() => step.run({})}>Apply<//>
    <${Outcome} step=${step} />
    ${doctor ? html`<${DoctorCard} doctor=${doctor} workspace=${state.workspace} />` : null}
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
  };
  useEffect(() => { refresh().catch((e) => setError(e.message)); }, []);
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
    ${state.steps.map((item) => {
      const View = VIEWS[item.name];
      return html`<article class=${"step " + item.state}>
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
    })}
  </section>`;
}
