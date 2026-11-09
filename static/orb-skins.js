// Orbs from two open-source libraries, both MIT (licences in static/vendor/orbs):
// - VoiceOrbs by Alexis Munoz (github.com/amunozdev/voiceorbs): 19 voice orbs to wear in
//   live mode in place of the lab's particle orb. They are React components; the bundle
//   carries React and loads the first time one is picked.
// - Thinking Orbs by Jakub Antalik (github.com/Jakubantalik/Libraries.dev): small dotted
//   orbs, one per kind of work, beside each tool step.
// The lab orb keeps running under a VoiceOrb: it still times the captions and follows the
// speech, and the worn orb gets its state and level from it each frame.

import { thinkingOrb } from "./vendor/orbs/thinking-orbs.js";

export const LAB = "lab";

// id and name, in the order of the VoiceOrbs gallery. Edge Glow is left out: it is a
// frame for other content, and live mode already glows at its edges.
export const ORB_CHOICES = [
  [LAB, "Lab particles"],
  ["plasma-orb", "Plasma"], ["equalizer-orb", "Equalizer"], ["halo-orb", "Halo"],
  ["particles-orb", "Particles"], ["siri-sheet", "Siri sheet"], ["siri-wave-line", "Siri wave line"],
  ["pulse-orb", "Pulse"], ["glass-orb", "Glass"], ["aurora-orb", "Aurora"], ["gooey-orb", "Gooey"],
  ["galaxy-orb", "Galaxy"], ["nebula-orb", "Nebula"], ["waveform-ring", "Waveform ring"],
  ["iridescent-flow", "Iridescent flow"], ["mercury-orb", "Mercury"], ["minimal-orb", "Minimal"],
  ["dither-orb", "Dither"], ["grain-orb", "Grain"], ["aura-field", "Aura field"],
];
export const isOrb = (id) => ORB_CHOICES.some(([k]) => k === id);

let bundle = null;
export function loadVoiceOrbs() {
  bundle ??= (async () => {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = new URL("./vendor/orbs/voiceorbs.css", import.meta.url).href;
    const css = new Promise((ok) => { link.onload = link.onerror = ok; });
    document.head.append(link);
    const mod = await import("./vendor/orbs/voiceorbs.js");
    await css;
    return mod;
  })();
  return bundle;
}

// The lab orb's ten states in VoiceOrbs' seven. While you talk and while it speaks, the orb
// gets the real level; otherwise it makes its own (level -1).
export const VO_STATE = {
  listening: "listening", hearing: "listening", gathering: "thinking", thinking: "thinking",
  searching: "thinking", reading: "thinking", speaking: "speaking", done: "idle",
  muted: "disabled", error: "error",
};

/** Dress the lab orb `orb` (a SpeakingOrb) in the VoiceOrb `id`, or take it off with LAB.
 *  `analyser()` gives the audio the waveform orbs should trace at that moment. */
export async function wear(orb, id, analyser = () => null) {
  const want = isOrb(id) ? id : LAB;
  orb.wanted = want;
  if (orb.skin?.id === want) return;
  orb.skin?.destroy();
  orb.skin = null;
  orb.root.classList.remove("skinned");
  if (want === LAB) return;
  let lib;
  try { lib = await loadVoiceOrbs(); }
  catch (err) { console.warn("VoiceOrbs did not load", err); return; }
  if (orb.wanted !== want) return; // picked something else while it loaded
  lib.useAnalyser(analyser);
  const host = document.createElement("div");
  host.className = "skin";
  host.setAttribute("aria-hidden", "true"); // the lab orb already says what state it's in
  orb.root.querySelector(".orb").append(host);
  const size = () => Math.max(40, Math.round(host.parentElement.getBoundingClientRect().width * 0.52));
  let shown = { state: "", size: size() };
  const mounted = lib.mountOrb(host, want, { state: "listening", size: shown.size });
  orb.root.classList.add("skinned");
  orb.skin = {
    id: want,
    // called by the lab orb on every frame instead of drawing its own particles
    frame(o) {
      const state = VO_STATE[o.state] || "listening";
      const live = state === "listening" ? o._level : state === "speaking" ? o.env : -1;
      mounted.levelRef.current = live < 0 ? -1 : Math.min(1, live);
      if (state !== shown.state) { shown.state = state; mounted.set({ state }); }
    },
    resize() {
      const s = size();
      if (Math.abs(s - shown.size) > 2) { shown.size = s; mounted.set({ size: s }); }
    },
    destroy() { mounted.destroy(); host.remove(); },
  };
}

// ---- Thinking Orbs: a small orb for each kind of work ---------------------------------
const VERB = {
  web_search: "searching", open_page: "connecting", click: "connecting", type_text: "connecting", go_back: "connecting",
  calculate: "solving", get_current_time: "working", add_reminder: "working", list_reminders: "working",
};
export const verbFor = (tool) => VERB[tool] || "working";

/** A Thinking Orb in a <canvas class="verb-orb">, `px` across, tinted with `color` [r, g, b].
 *  It stops by itself once it's taken off the page. */
export function verbOrb(verb, px = 22, color) {
  const canvas = document.createElement("canvas");
  canvas.className = "verb-orb";
  canvas.setAttribute("aria-hidden", "true");
  const handle = thinkingOrb(canvas, { state: verb, size: px, color, dotSize: 1.35 }); // a little heavier, to read at this size
  canvas.orb = handle;
  return canvas;
}
