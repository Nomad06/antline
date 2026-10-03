import { html, useEffect, useRef, useState } from "../vendor/htm-preact.mjs";
import { get, post } from "../api.js";
import { Button, Notice } from "./controls.js";

const STAMP = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(\d{3})\d{3}Z$/;

function when(version) {
  const m = STAMP.exec(version);
  return m ? m[1] + "-" + m[2] + "-" + m[3] + " " + m[4] + ":" + m[5] + ":" + m[6] + "." + m[7] + " UTC" : version;
}

export function History({ fileKey, stamp, onRestored }) {
  const [versions, setVersions] = useState(null);
  const [chosen, setChosen] = useState(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [problem, setProblem] = useState(null);
  const [restored, setRestored] = useState(null);
  const current = useRef(fileKey);
  const loadedFor = useRef(null);
  const wanted = useRef(null);
  current.current = fileKey;
  const query = "?key=" + encodeURIComponent(fileKey);
  const load = (forKey) => get("/api/history?key=" + encodeURIComponent(forKey)).then(
    (r) => { if (current.current === forKey) { setError(null); setVersions(r.versions); } },
    (e) => { if (current.current === forKey) { setError(e.message); } });
  // a new stamp means the page saved the file: the list reloads, what the reader chose stays
  useEffect(() => {
    if (loadedFor.current !== fileKey) {
      loadedFor.current = fileKey;
      wanted.current = null;
      setVersions(null);
      setChosen(null);
      setText("");
      setError(null);
      setProblem(null);
      setRestored(null);
    }
    load(fileKey);
  }, [fileKey, stamp]);
  const show = (version) => {
    wanted.current = version;
    setProblem(null);
    setRestored(null);
    get("/api/history/version" + query + "&version=" + encodeURIComponent(version)).then(
      (r) => { if (wanted.current === version) { setChosen(version); setText(r.text); } },
      (e) => { if (wanted.current === version) { setProblem(e.message); } });
  };
  const restore = async () => {
    const version = chosen;
    setBusy(true);
    setProblem(null);
    try {
      const now = await get("/api/files" + query);
      await post("/api/history/restore", { key: fileKey, version, hash: now.hash });
      wanted.current = null;
      setChosen(null);
      setText("");
      setRestored("Restored the version of " + when(version) + ". The file it replaced is in the list now.");
      await load(fileKey);
      if (onRestored) {
        onRestored();
      }
    } catch (e) {
      setProblem(e.message);
    }
    setBusy(false);
  };
  if (error) {
    return html`<${Notice} kind="error">${error}<//>`;
  }
  if (!versions) {
    return html`<p class="muted">Loading the history...</p>`;
  }
  return html`<div class="history">
    ${versions.length ? null : html`<p class="muted">No earlier version of this file yet.</p>`}
    ${versions.map((item) => html`<button class=${item.version === chosen ? "primary" : ""} aria-pressed=${item.version === chosen}
      onClick=${() => show(item.version)}>${when(item.version)}</button>`)}
    ${chosen ? html`<pre>${text || "(empty)"}</pre>
      <${Button} busy=${busy} onClick=${restore}>Restore the version of ${when(chosen)}<//>` : null}
    ${restored ? html`<${Notice} kind="ok">${restored}<//>` : null}
    ${problem ? html`<${Notice} kind="error">${problem}<//>` : null}
  </div>`;
}
