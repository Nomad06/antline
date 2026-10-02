import { html, render, useEffect, useState } from "./vendor/htm-preact.mjs";
import { HomePage } from "./pages/home.js";
import { SetupPage } from "./pages/setup.js";

const PAGES = { "": HomePage, setup: SetupPage };
const NAV = [{ route: "", label: "Home" }, { route: "setup", label: "Setup" }];

function currentRoute() {
  return window.location.hash.replace(/^#\/?/, "").split("?")[0];
}

function readMode() {
  try {
    return window.localStorage.getItem("ppl-mode") === "expert" ? "expert" : "simple";
  } catch (e) {
    return "simple";
  }
}

function writeMode(mode) {
  try {
    window.localStorage.setItem("ppl-mode", mode);
  } catch (e) {
    return;
  }
}

function App() {
  const [route, setRoute] = useState(currentRoute());
  const [mode, setMode] = useState(readMode());
  const [lost, setLost] = useState(false);
  useEffect(() => {
    const onHash = () => setRoute(currentRoute());
    const onLost = () => setLost(true);
    window.addEventListener("hashchange", onHash);
    window.addEventListener("ppl-session-lost", onLost);
    return () => {
      window.removeEventListener("hashchange", onHash);
      window.removeEventListener("ppl-session-lost", onLost);
    };
  }, []);
  if (lost) {
    return html`<main class="lost">
      <h1>The studio stopped</h1>
      <p>Run <code>ppl serve --open</code> again and use the link it prints.</p>
    </main>`;
  }
  const Page = Object.hasOwn(PAGES, route) ? PAGES[route] : PAGES[""];
  const toggle = () => {
    const next = mode === "simple" ? "expert" : "simple";
    writeMode(next);
    setMode(next);
  };
  return html`
    <header class="bar">
      <a class="brand" href="#/">antline studio</a>
      <nav>
        ${NAV.map((item) => html`<a class=${route === item.route ? "on" : ""} href=${"#/" + item.route}>${item.label}</a>`)}
        <a href="/runs">Runs</a>
      </nav>
      <button onClick=${toggle} aria-pressed=${mode === "expert"} title="Expert mode adds the raw files and the cage playground">
        ${mode === "simple" ? "Simple" : "Expert"} mode
      </button>
    </header>
    <main><${Page} mode=${mode} /></main>`;
}

render(html`<${App} />`, document.getElementById("app"));
