import { html, useRef, useState } from "../vendor/htm-preact.mjs";
import { post, runJob } from "../api.js";
import { Button, Field, Log, Notice } from "../ui/controls.js";
import { HarnessForm } from "../ui/harness-form.js";

export function HarnessStep({ mode, done, onInstall }) {
  const [path, setPath] = useState("");
  const [info, setInfo] = useState(null);
  const [chosen, setChosen] = useState([]);
  const [draft, setDraft] = useState(null);
  const [extras, setExtras] = useState({ notes: [], dropped: [] });
  const [version, setVersion] = useState(0);
  const [busy, setBusy] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [error, setError] = useState(null);
  const [log, setLog] = useState([]);
  const [reused, setReused] = useState(null);
  const latest = useRef(0);

  const show = (next, found) => {
    setDraft(next);
    setReused(null);
    setExtras(found);
    setVersion((n) => n + 1);
  };
  const inspect = async () => {
    const mine = ++latest.current;
    setBusy(true);
    setError(null);
    setDraft(null);
    try {
      const found = await post("/api/project/inspect", { path });
      if (mine === latest.current) {
        setInfo(found);
        setChosen(found.md_files.map((file) => file.path));
      }
    } catch (e) {
      if (mine === latest.current) {
        setError(e.message);
      }
    }
    if (mine === latest.current) {
      setBusy(false);
    }
  };
  const loadPreset = async (reason) => {
    const mine = ++latest.current;
    setBusy(true);
    setError(null);
    try {
      const preset = await post("/api/harness/preset", { path: info.path });
      if (mine === latest.current) {
        show(preset,
          { notes: reason ? ["Claude could not draft it, so the preset is used instead: " + reason] : [], dropped: [] });
      }
    } catch (e) {
      if (mine === latest.current) {
        setError(e.message);
      }
    }
    if (mine === latest.current) {
      setBusy(false);
    }
  };
  const extract = async (options) => {
    const mine = ++latest.current;
    setBusy(true);
    setError(null);
    setLog([]);
    try {
      const merged = await runJob(post("/api/harness/extract", { path: info.path, md: chosen, fresh: Boolean(options && options.fresh) }),
        (lines) => {
          if (mine === latest.current) {
            setLog((old) => old.concat(lines));
          }
        });
      if (mine === latest.current) {
        show(merged, { notes: merged.notes || [], dropped: merged.dropped || [] });
        setReused(merged.cached ? merged.cached_at || "" : null);
        setBusy(false);
      }
    } catch (e) {
      if (mine !== latest.current) {
        return;
      }
      if (e.code === "EXTRACTION_FAILED") {
        await loadPreset(e.message);
      } else {
        setError(e.message);
        setBusy(false);
      }
    }
  };
  const install = async (form) => {
    if (onInstall) {
      await onInstall(form);
      return;
    }
    setInstalling(true);
    try {
      const result = await runJob(post("/api/harness/draft/install", form), (lines) => setLog((old) => old.concat(lines)));
      if (result.failed && result.failed.length) {
        throw new Error(result.reasons.harness || "the harness step did not complete");
      }
      await done(result);
    } finally {
      setInstalling(false);
    }
  };
  const locked = busy || installing;
  const toggle = (file, on) => setChosen(on ? chosen.concat([file]) : chosen.filter((item) => item !== file));

  return html`<div>
    <${Field} label="Project folder" help="The git checkout the pipeline works on. A subfolder is fine, the checkout root is found.">
      <input value=${path} placeholder="~/src/my-app" disabled=${locked} onInput=${(e) => setPath(e.target.value)} /><//>
    <${Button} busy=${busy && !info} disabled=${!path.trim() || locked} onClick=${inspect}>Look at the project<//>
    ${error ? html`<${Notice} kind="error">${error}<//>` : null}
    ${info ? html`<div class="card">
      <p><strong>${info.path}</strong>: default branch <code>${info.default_branch}</code>, ${info.stack.label}${info.forge_hint ? ", forge " + info.forge_hint : ""}.</p>
      ${info.md_files.length ? html`
        <p>These files describe the project. Claude can read them and draft the harness, for a few cents:</p>
        ${info.md_files.map((file) => html`<label class="check">
          <input type="checkbox" checked=${chosen.includes(file.path)} onChange=${(e) => toggle(file.path, e.target.checked)} />
          ${file.path} <span class="muted">${file.bytes} bytes</span></label>`)}
        <${Button} primary busy=${busy} disabled=${!chosen.length || installing} onClick=${() => extract()}>Draft it with Claude<//>
        <${Button} disabled=${locked} onClick=${() => loadPreset(null)}>Use the ${info.stack.label} preset<//>` : html`
        <p>No CLAUDE.md or AGENTS.md here, so the harness starts from the ${info.stack.label} preset.</p>
        <${Button} primary busy=${busy} disabled=${installing} onClick=${() => loadPreset(null)}>Start from the preset<//>`}
      <${Log} lines=${log} />
    </div>` : null}
    ${draft && reused !== null ? html`<${Notice}>Reused the draft from ${reused.slice(0, 10)}, no cost.
      <${Button} disabled=${locked} onClick=${() => extract({ fresh: true })}>Draft again (new call)<//><//>` : null}
    ${draft ? html`<${HarnessForm} key=${version} draft=${draft} extras=${extras} mode=${mode} saveLabel="Install the harness" onSave=${install} />` : null}
  </div>`;
}
