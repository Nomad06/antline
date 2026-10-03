import { html, useEffect, useRef, useState } from "../vendor/htm-preact.mjs";
import { get, post, put, runJob } from "../api.js";
import { Badge, Button, Field, Log, Notice } from "../ui/controls.js";
import { HarnessForm } from "../ui/harness-form.js";
import { History } from "../ui/history.js";
import { HarnessStep } from "./setup-harness.js";

const isTable = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const isList = (value) => value === undefined || Array.isArray(value);

// A harness.toml that parses but lacks what the form reads is repaired in the raw editor instead
function formable(doc) {
  return isTable(doc) && ["harness", "repo", "task", "forge"].every((key) => isTable(doc[key]))
    && isList(doc.gate) && (doc.env === undefined || isTable(doc.env))
    && (doc.guard === undefined || (isTable(doc.guard) && isList(doc.guard.shape)));
}

export function HarnessPage({ mode }) {
  const [list, setList] = useState(null);
  const [name, setName] = useState(null);
  const [current, setCurrent] = useState(null);
  const [raw, setRaw] = useState("");
  const [creating, setCreating] = useState(false);
  const [source, setSource] = useState("");
  const [problem, setProblem] = useState(null);
  const [message, setMessage] = useState(null);
  const [busy, setBusy] = useState(null);
  const [log, setLog] = useState([]);
  const latest = useRef(0);
  const say = (text) => { setProblem(null); setMessage(text); };
  const fail = (e) => { setMessage(null); setProblem(e.message); };
  const act = async (kind, work) => {
    setBusy(kind);
    try {
      await work();
    } catch (e) {
      fail(e);
    }
    setBusy(null);
  };
  const onLog = (lines) => setLog((old) => old.concat(lines));
  const open = (which) => {
    const mine = ++latest.current;
    setName(which);
    setCreating(false);
    setCurrent((old) => (old && old.name === which ? old : null));
    return get("/api/harnesses/" + encodeURIComponent(which)).then((data) => {
      if (mine === latest.current) {
        setCurrent(data);
        setRaw(data.text);
      }
    }, (e) => { if (mine === latest.current) { fail(e); } });
  };
  const loadList = () => get("/api/harnesses").then((data) => {
    setList(data);
    return data;
  }, (e) => { fail(e); return null; });
  useEffect(() => {
    loadList().then((data) => {
      if (data && data.names.length) {
        open(data.names.includes(data.active) ? data.active : data.names[0]);
      }
    });
  }, []);
  if (!list) {
    return problem ? html`<${Notice} kind="error">${problem}<//>` : html`<p class="muted">Loading...</p>`;
  }
  const active = list.active === name;
  const url = (suffix) => "/api/harnesses/" + encodeURIComponent(name) + (suffix || "");
  const save = async (form) => {
    await put(url(), { doc: form.doc, fragments: form.fragments, hashes: current.hashes });
    say("Saved. Apply to render the agents and the skill from it.");
    open(name);
  };
  const saveRaw = () => act("raw", async () => {
    await put(url(), { text: raw, hashes: current.hashes });
    say("Saved the file as written. Apply to render the agents and the skill from it.");
    await open(name);
  });
  const apply = () => act("apply", async () => {
    setLog([]);
    await runJob(post(url("/apply")), onLog);
    say("Applied: the agents and the skill come from " + name + " now.");
    await loadList();
  });
  const add = () => act("add", async () => {
    setLog([]);
    const added = await runJob(post("/api/harnesses", { source: source.trim() }), onLog);
    say("Added " + added.name + ". Apply it to make it the active harness.");
    setSource("");
    await loadList();
    await open(added.name);
  });
  const create = async (form) => {
    const made = await post("/api/harnesses/new", form);
    say("Created " + made.name + ". Apply it to make it the active harness.");
    await loadList();
    await open(made.name);
  };
  const form = current && formable(current.doc);
  const whyNoForm = current && current.check.errors.length ? current.check.errors.join(" ")
    : "This harness.toml cannot be shown as a form: edit the file as written below.";
  return html`<section>
    <h1>Harnesses</h1>
    <p class="muted">A harness holds everything about one project: its gates, the commands the test writer may run, the tracker and the forge.</p>
    <div>${list.names.map((item) => html`<button class=${item === name && !creating ? "primary" : ""} onClick=${() => open(item)}>
      ${item}${item === list.active ? " (active)" : ""}</button>`)}
      <button class=${creating ? "primary" : ""} onClick=${() => setCreating(true)}>New, for another project</button></div>
    ${list.names.length ? null : html`<p class="muted">No harness is installed yet. Add one below, or create one for a project.</p>`}
    <div class="grid">
      <${Field} label="Add from a git URL or a directory" help="A git URL, or the absolute path of a harness directory, for example ~/src/antline/defaults/harnesses/example-maven">
        <input value=${source} onInput=${(e) => setSource(e.target.value)} /><//>
    </div>
    <${Button} busy=${busy === "add"} disabled=${!source.trim() || Boolean(busy)} onClick=${add}>Add<//>
    ${message ? html`<${Notice} kind="ok">${message}<//>` : null}
    ${problem ? html`<${Notice} kind="error">${problem}<//>` : null}
    <${Log} lines=${log} />
    ${creating ? html`<${HarnessStep} mode=${mode} onInstall=${create} />` : null}
    ${current && !creating ? html`<div>
      <h2>${name} ${active ? html`<${Badge} kind="done">active<//>` : null}</h2>
      <${Button} primary=${!active} busy=${busy === "apply"} disabled=${Boolean(busy)} onClick=${apply}>${active ? "Apply again" : "Apply and make it active"}<//>
      <p class="help">Apply writes the six agent files and the skill from the saved harness and makes it the active one, as <code>ppl harness apply</code> does.</p>
      ${form ? html`<${HarnessForm} key=${name + ":" + Object.values(current.hashes).join("")}
        draft=${{ doc: current.doc, fragments: current.fragments, sources: {} }} extras=${{}}
        mode=${mode} saveLabel="Save" nameLocked=${true} onSave=${save} />` : html`<${Notice} kind="error">${whyNoForm}<//>`}
      ${mode === "expert" || !form ? html`<h3>harness.toml as written</h3>
        <textarea aria-label="harness.toml as written" value=${raw} onInput=${(e) => setRaw(e.target.value)}></textarea>
        <${Button} busy=${busy === "raw"} disabled=${raw === current.text || Boolean(busy)} onClick=${saveRaw}>Save the file as written<//>` : null}
      ${mode === "expert" ? html`<h3>History of harness.toml</h3>
        <${History} fileKey=${"harness:" + name + ":harness.toml"} stamp=${current.hashes["harness.toml"]} onRestored=${() => open(name)} />` : null}
    </div>` : null}
  </section>`;
}
