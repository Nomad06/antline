import { html } from "../vendor/htm-preact.mjs";

// One worker ant in side view, walking right, about 32 by 20 units with its
// feet on y = 19. `leaf` puts a cut leaf in its mandibles.
export function Ant({ kind, leaf }) {
  return html`<g class=${"ant " + (kind || "done")}>
    ${leaf ? html`<g class="leaf">
      <path d="M27 8 C23 1 25 -6 31 -9 C34 -3 32 4 27 8 Z" />
      <path class="rib" d="M27.4 7 L30.6 -7" />
    </g>` : null}
    <g class="limbs">
      <path d="M20.5 11 L23.5 14.5 L26 19" />
      <path d="M18.6 11.6 L18.9 15.6 L17 19" />
      <path d="M16.8 11 L13 14.6 L10.4 19" />
      <path d="M25.6 6.2 L27.3 2.6 L31 3.4" />
    </g>
    <ellipse class="body" cx="8.6" cy="10.6" rx="6.2" ry="4.6" />
    <circle class="body" cx="15.4" cy="10.7" r="1.5" />
    <ellipse class="body" cx="19.2" cy="9.9" rx="3.6" ry="2.4" />
    <circle class="body" cx="25" cy="8.7" r="3" />
  </g>`;
}

export function AntMark() {
  return html`<svg class="ant-mark" viewBox="0 -1 33 21" width="30" height="19" aria-hidden="true">
    <${Ant} kind="done" />
  </svg>`;
}

const TRAIL_WIDTH = 720;
const GROUND = 100;
const NEST_X = 668;
const SCALE = 1.5;

// The six setup steps as six ants on the trail to the nest: a done step's ant
// carries a leaf, the next step's ant is amber, a step still to do is drawn in
// outline.
export function AntTrail({ steps, next }) {
  const spacing = (NEST_X - 70) / steps.length;
  const done = steps.filter((step) => step.state === "done").length;
  const label = done + " of " + steps.length + " setup steps are done";
  return html`<svg class="trail-art" viewBox=${"0 44 " + TRAIL_WIDTH + " 96"} role="img" aria-label=${label}>
    <path class="trail-line" d=${"M6 " + GROUND + " L" + (NEST_X - 30) + " " + GROUND} />
    <path class="nest" d=${"M" + (NEST_X - 44) + " " + GROUND + " Q" + NEST_X + " " + (GROUND - 58) + " " + (NEST_X + 44) + " " + GROUND + " Z"} />
    <ellipse class="nest-door" cx=${NEST_X} cy=${GROUND - 13} rx="8" ry="6" />
    ${steps.map((step, i) => {
      const x = 22 + i * spacing;
      const kind = step.state === "done" ? "done" : step.name === next ? "next" : step.state === "skipped" ? "skipped" : "todo";
      return html`<g transform=${"translate(" + x + " " + (GROUND - 19 * SCALE - 1) + ") scale(" + SCALE + ")"}>
        <g class="walker" style=${"animation-delay: " + i * 70 + "ms"}>
          <${Ant} kind=${kind} leaf=${step.state === "done"} />
        </g>
      </g>
      <text class=${"trail-label " + kind} x=${x + 16 * SCALE} y=${GROUND + 26} text-anchor="middle">${step.name}</text>`;
    })}
  </svg>`;
}
