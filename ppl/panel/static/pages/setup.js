import { html, useEffect, useState } from "../vendor/htm-preact.mjs";
import { get, post } from "../api.js";
import { Badge, Button, Field, Log, Notice } from "../ui/controls.js";
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
    <p>antline keeps its settings, verified models and harnesses in <code>${state.data_root}</code>. The code is in <code>${state.code_root || "unknown"}</code>.</p>
    <${Button} primary busy=${step.busy} onClick=${() => step.run({})}>Create the data root<//>
    <${Outcome} step=${step} />
  </div>`;
}

const HOOK_LABELS = {
  local: "Only in this workspace (recommended): written into its .claude/settings.local.json",
  global: "In all my Claude Code projects: prints the entries to paste into the global settings file, nothing is registered until you paste them",
  manual: "Print the entries, I paste them into the settings file I choose: nothing is registered until I do",
};

// The lines from "add these entries to <file>:" on, the step's own log of what to paste.
function printedEntries(lines) {
  const start = lines.findIndex((line) => line.startsWith("add these entries to"));
  return start < 0 ? null : lines.slice(start);
}

function HookWarning({ lines }) {
  return html`<${Notice} kind="warn">The guard hook is not registered: agents run without the cage until the entries are pasted.
    ${lines ? html`<pre class="log">${lines.join("\n")}</pre>`
      : html`<br />Run the Claude Code step again to print the entries, or to register them in the workspace.`}<//>`;
}

function ClaudeStep({ state, mode, done, onHooks }) {
  const step = useStepRunner("claude", async (result, lines) => {
    await onHooks(printedEntries(lines));
    await done();
  });
  const [binary, setBinary] = useState("claude");
  const [workspace, setWorkspace] = useState(state.workspace || "~/ppl-workspace");
  const [hooks, setHooks] = useState("local");
  const chosen = mode === "expert" ? hooks : "local";
  return html`<div>
    <${Field} label="Workspace folder" help="Pipeline sessions start here. Its .claude folder gets the agents, the skill and the hooks.">
      <input value=${workspace} onInput=${(e) => setWorkspace(e.target.value)} /><//>
    ${mode === "expert" ? html`<${Field} label="Claude Code binary"><input value=${binary} onInput=${(e) => setBinary(e.target.value)} /><//>
      <fieldset><legend>Where the guard hooks go</legend>
        ${Object.keys(HOOK_LABELS).map((choice) => html`<label><input type="radio" name="hooks" checked=${hooks === choice} onChange=${() => setHooks(choice)} />${HOOK_LABELS[choice]}</label>`)}
      </fieldset>`
      : html`<p class="help">The guard hooks are registered in the workspace, in its .claude/settings.local.json.</p>`}
    <${Button} primary busy=${step.busy} disabled=${!workspace.trim()} onClick=${() => step.run({ claude_binary: binary, workspace, hooks: chosen })}>${chosen === "local" ? "Save and register the hooks" : "Save and print the entries"}<//>
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

const REGISTRATION_WORDS = { local: "local", global: "global", both: "local and global",
                             none: "not registered" };

function hookRuns(doctor) {
  if (doctor.hook_runs === true) {
    return "yes";
  }
  if (doctor.hook_runs === false) {
    return "no: " + (doctor.hook_runs_problem || "the registered hook did not run clean");
  }
  return "not registered";
}

function DoctorCard({ doctor, workspace, hookLines }) {
  return html`<div class="card">
    <h3>Ready check</h3>
    <ul>
      <li>Guard hook runs: ${hookRuns(doctor)}</li>
      <li>Hook registration: ${REGISTRATION_WORDS[doctor.registration] || "unknown: a settings file could not be read"}</li>
      <li>Active harness: ${(doctor.harness && doctor.harness.active) || "none"}</li>
    </ul>
    ${doctor.registration === "none" ? html`<${HookWarning} lines=${hookLines} />` : null}
    <h3>Start your first task</h3>
    <p>Open Claude Code in <code>${workspace || "your workspace"}</code> and type the task's key after the skill name, a tracker link works too:</p>
    <pre>/pipeline KEY-123</pre>
  </div>`;
}

function ApplyStep({ state, done, hooks }) {
  const [doctor, setDoctor] = useState(null);
  const step = useStepRunner("apply", async () => {
    await done();
    setDoctor(await get("/api/doctor"));
  });
  return html`<div>
    <p>Writes the six agent files and the orchestrator skill into the workspace and makes the harness active.</p>
    <${Button} primary busy=${step.busy} onClick=${() => step.run({})}>Apply<//>
    <${Outcome} step=${step} />
    ${doctor ? html`<${DoctorCard} doctor=${doctor} workspace=${state.workspace} hookLines=${hooks && hooks.lines} />` : null}
  </div>`;
}

const VIEWS = { paths: PathsStep, claude: ClaudeStep, models: ModelsStep, harness: HarnessStep,
                forge: ForgeStep, apply: ApplyStep };

export function SetupPage({ mode }) {
  const [state, setState] = useState(null);
  const [open, setOpen] = useState(null);
  const [error, setError] = useState(null);
  const [problem, setProblem] = useState(null);
  const [hooks, setHooks] = useState(null);
  const refresh = async () => {
    const fresh = await get("/api/setup");
    setState(fresh);
    setOpen(fresh.next || "apply");
    return fresh;
  };
  // the registration as the doctor sees it, with the entries the Claude Code step printed
  const checkHooks = (lines) => get("/api/doctor").then(
    (doctor) => setHooks({ registration: doctor.registration, lines: lines || null }),
    () => setHooks(null));
  useEffect(() => {
    refresh().then((fresh) => {
      if (fresh.steps.some((item) => item.name === "claude" && item.state === "done")) {
        checkHooks(null);
      }
    }).catch((e) => setError(e.message));
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
        ${item.name === "claude" && hooks && hooks.registration === "none" ? html`<${HookWarning} lines=${hooks.lines} />` : null}
        ${open === item.name ? html`<div class="body">
          <p class="muted">${item.intro}</p>
          <${View} state=${state} mode=${mode} done=${refresh} hooks=${hooks} onHooks=${checkHooks} />
          ${problem && problem.step === item.name ? html`<${Notice} kind="error">${problem.message}<//>` : null}
          <p class="skip"><a href="#/setup" onClick=${(e) => { e.preventDefault(); skip(item.name); }}>Skip, set this up later</a></p>
        </div>` : null}
      </article>`;
    })}</div>
  </section>`;
}
