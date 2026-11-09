// LFM3-Omni: one conversation that takes text, images, audio, video and speech, answers
// in text or speech, calls local and web tools, and has a hands-free live mode.
// No build step, no dependencies.

import { SpeakingOrb, estimateSeconds, pitchOf } from "./orb.js";
import { wear, ORB_CHOICES, LAB, verbOrb, verbFor } from "./orb-skins.js";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------- formatters ----------
const fmtGB = (gb) => (gb == null ? "–" : `${gb.toFixed(1)} GB`);
function fmtDuration(s) {
  if (s == null) return "–";
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}
const fmtClock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const hostOf = (url) => { try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return url; } };

const FINISH_NOTES = {
  length: "hit the length limit",
  cancelled: "stopped early",
  noise: "speech cut where it turned to noise",
  overrun: "speech cut, it ran past the text",
};

function statsLines(s) {
  if (!s) return [];
  const lines = [];
  if (s.audio_seconds) lines.push(`${s.audio_seconds.toFixed(1)} s of speech`);
  else if (s.tokens >= 4 && s.tokens_per_sec) lines.push(`${s.tokens} tokens, ${Math.round(s.tokens_per_sec)}/s`);
  lines.push(`took ${s.seconds.toFixed(1)} s`);
  if (FINISH_NOTES[s.finish]) lines.push(FINISH_NOTES[s.finish]);
  return lines;
}

// ---------- tiny, safe markdown ----------
const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const inline = (s) =>
  s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\s][^*]*)\*(?!\w)/g, "$1<em>$2</em>");

function md(text) {
  const lines = text.replace(/\r/g, "").split("\n");
  let html = "", list = null, code = null;
  const closeList = () => { if (list) html += `</${list}>`; list = null; };
  for (const line of lines) {
    if (code !== null) {
      if (/^\s*```/.test(line)) { html += `<pre><code>${esc(code.join("\n"))}</code></pre>`; code = null; }
      else code.push(line);
      continue;
    }
    let m;
    if (/^\s*```/.test(line)) { closeList(); code = []; continue; }
    if ((m = line.match(/^\s*[-*•]\s+(.*)/))) {
      if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; }
      html += `<li>${inline(esc(m[1]))}</li>`;
      continue;
    }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) {
      if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; }
      html += `<li>${inline(esc(m[1]))}</li>`;
      continue;
    }
    closeList();
    if ((m = line.match(/^#{1,6}\s+(.*)/))) { html += `<p><strong>${inline(esc(m[1]))}</strong></p>`; continue; }
    // The model separates paragraphs with single newlines, so each line is its own paragraph.
    if (line.trim()) html += `<p>${inline(esc(line))}</p>`;
  }
  if (code !== null) html += `<pre><code>${esc(code.join("\n"))}</code></pre>`;
  closeList();
  return html;
}

async function* ndjson(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let i;
    while ((i = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, i).trim();
      buffer = buffer.slice(i + 1);
      if (line) yield JSON.parse(line);
    }
  }
  if (buffer.trim()) yield JSON.parse(buffer);
}

async function errorFrom(response) {
  try { return (await response.json()).error || `The request failed (HTTP ${response.status}).`; }
  catch { return `The request failed (HTTP ${response.status}).`; }
}

// ---------- saved preferences ----------
const prefs = {
  get(key, fallback) {
    try { const v = localStorage.getItem(`lfm3.${key}`); return v == null ? fallback : JSON.parse(v); }
    catch { return fallback; }
  },
  set(key, value) { try { localStorage.setItem(`lfm3.${key}`, JSON.stringify(value)); } catch {} },
};

// ---------- spoken replies: streamed 16-bit PCM, played as it arrives ----------
// One player for the page, so a new reply always silences the last one. Its analyser
// drives the live orb, and heard() is the clock for the live captions.
class StreamPlayer {
  constructor() { this.ctx = null; this.state = "idle"; this.session = 0; this.segments = []; this.sources = []; }
  // Called from a click or key press, so the browser lets the page play sound later.
  prime() {
    if (!this.ctx) {
      this.ctx = new AudioContext();
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 1024;
      this.analyser.connect(this.ctx.destination);
    }
    if (this.ctx.state === "suspended") this.ctx.resume().catch(() => {});
  }
  get active() { return this.state === "playing"; }
  begin(onEnd) {
    this.stop();
    this.prime();
    this.session += 1;
    this.state = "playing";
    this.segments = [];
    this.sources = [];
    this.until = 0;
    this.finished = false;
    this.onEnd = onEnd;
    return this.session;
  }
  _add(session, buffer) {
    if (session !== this.session || this.state !== "playing") return;
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.analyser);
    // a short cushion before the first piece, so the next one is here before it runs out
    const when = Math.max(this.until, this.ctx.currentTime + (this.segments.length ? 0.03 : 0.15));
    src.start(when);
    this.segments.push({ when, dur: buffer.duration });
    this.until = when + buffer.duration;
    this.sources.push(src);
    src.onended = () => this._check();
  }
  push(session, b64, rate) {
    if (session !== this.session || this.state !== "playing") return;
    const bin = atob(b64), bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const pcm = new Int16Array(bytes.buffer, 0, bytes.length >> 1);
    const buffer = this.ctx.createBuffer(1, pcm.length || 1, rate);
    const ch = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
    this._add(session, buffer);
  }
  async playUrl(url, onEnd) {
    const session = this.begin(onEnd);
    try {
      const data = await (await fetch(url)).arrayBuffer();
      this._add(session, await this.ctx.decodeAudioData(data));
    } catch {}
    this.finish(session);
    return session;
  }
  finish(session) {
    if (session !== this.session) return;
    this.finished = true;
    this._check();
  }
  get seconds() { return this.segments.reduce((s, g) => s + g.dur, 0); }
  heard() {
    if (!this.ctx) return 0;
    const now = this.ctx.currentTime;
    return this.segments.reduce((s, g) => s + Math.min(g.dur, Math.max(0, now - g.when)), 0);
  }
  _check() {
    if (this.state !== "playing" || !this.finished) return;
    if (this.segments.length && this.ctx.currentTime < this.until - 0.02) return;
    this._end(true);
  }
  _end(natural) {
    this.state = "idle";
    const cb = this.onEnd;
    this.onEnd = null;
    cb?.(natural);
  }
  stop() {
    if (this.state !== "playing") return;
    for (const s of this.sources) { try { s.onended = null; s.stop(); } catch {} }
    this.sources = [];
    this._end(false);
  }
}
const player = new StreamPlayer();

// ---------- model status ----------
let status = { state: "stopped" };
let controlPending = false;
const isReady = () => status.state === "ready";

const STATE_TEXT = {
  stopped: ["Stopped", () => "Not loaded, so it uses no memory."],
  starting: ["Loading", (s) => `Loading the model into memory${s.starting_seconds ? `, ${s.starting_seconds} s so far` : ""}.`],
  ready: ["Ready", () => "Loaded and waiting for you."],
  busy: ["Working", () => "Answering."],
  stopping: ["Stopping", () => "Releasing the model's memory."],
  error: ["Stopped with an error", (s) => s.error || "Something went wrong. Check the model log."],
};

function renderStatus() {
  const visual = status.state === "ready" && status.busy ? "busy" : status.state;
  document.body.dataset.state = visual;
  const [word, detail] = STATE_TEXT[visual] || STATE_TEXT.stopped;
  $("#state-word").textContent = word;
  $("#state-detail").textContent = detail(status);
  if (status.model) $("#model-id").textContent = status.model;
  // The model's own count (MLX's, or the GPU memory llama.cpp took): the process RSS leaves out
  // the GPU-side buffers that hold the weights.
  $("#fact-mem").textContent = fmtGB(status.memory_gb);
  $("#fact-load").textContent = status.load_seconds != null ? `${status.load_seconds} s` : "–";
  $("#fact-up").textContent = fmtDuration(status.uptime_seconds);

  const running = ["starting", "ready"].includes(status.state);
  $("#btn-start").hidden = running || status.state === "stopping";
  $("#btn-stop").hidden = !running;
  $("#btn-restart").hidden = !running;
  for (const b of $$("#btn-start, #btn-stop, #btn-restart, [data-action='start']")) b.disabled = controlPending;

  const gate = $("#gate");
  const gateText = $("span", gate);
  const gateButton = $("button", gate);
  gateButton.hidden = !["stopped", "error"].includes(status.state);
  gateButton.textContent = status.state === "error" ? "Start again" : "Start model";
  let note = {
    stopped: "The model is stopped. Start it to begin.",
    starting: "The model is loading. It's usually ready in a few seconds; the first run also downloads it.",
    stopping: "The model is stopping.",
    error: status.error || "The model stopped with an error.",
  }[status.state] || "";
  if (status.state === "ready" && status.ffmpeg === false) {
    note = "ffmpeg wasn't found, so audio, recording and video won't work. Install it with: brew install ffmpeg";
  }
  gateText.textContent = note;
  gate.hidden = !note;
  if (isReady() && noteWaitsForModel) updateFootNote();
  if (live.open && !running && status.state !== "stopping") live.modelGone();
  refreshControls();
}

async function pollStatus() {
  try {
    status = await (await fetch("/api/model")).json();
  } catch {
    status = { state: "error", error: "The app server isn't responding. Run start.sh again." };
  }
  renderStatus();
}

async function control(action) {
  controlPending = true;
  renderStatus();
  try {
    status = await (await fetch(`/api/model/${action}`, { method: "POST" })).json();
  } catch {}
  controlPending = false;
  renderStatus();
}

$("#btn-start").addEventListener("click", () => control("start"));
$("#btn-stop").addEventListener("click", () => control("stop"));
$("#btn-restart").addEventListener("click", () => control("restart"));
$("[data-action='start']").addEventListener("click", () => control("start"));

// ---------- options, settings, tools ----------
let options = { default_system: "", tools: [], tts_languages: [] };
const TOOL_NAMES = {
  get_current_time: "Clock",
  calculate: "Calculator",
  add_reminder: "Save a reminder",
  list_reminders: "List reminders",
  web_search: "Web search",
  open_page: "Open a page",
  click: "Click",
  type_text: "Type",
  go_back: "Go back",
};
const TOOL_PENDING = {
  get_current_time: "Checking the clock",
  calculate: "Calculating",
  add_reminder: "Saving a reminder",
  list_reminders: "Looking up your reminders",
  web_search: "Searching the web",
  open_page: "Opening a page",
  click: "Clicking",
  type_text: "Typing",
  go_back: "Going back",
};
const TOOL_LABELS = {
  get_current_time: "Checked the clock",
  calculate: "Calculated",
  add_reminder: "Saved a reminder",
  list_reminders: "Looked up your reminders",
  web_search: "Searched the web",
  open_page: "Opened a page",
  click: "Clicked",
  type_text: "Typed",
  go_back: "Went back",
};
const WEB_TOOLS = new Set(["web_search", "open_page", "click", "type_text", "go_back"]);

const systemBox = $("#system");
const tempInput = $("#temperature");
const maxTokensInput = $("#max-tokens");
const toolsSwitch = $("#tools-on");
const webSwitch = $("#web-on");
const liveWeb = $("#live-web");

async function loadOptions() {
  try { options = await (await fetch("/api/options")).json(); } catch { return; }
  // "Mac" or "PC": where the model runs, in the page's wording.
  for (const el of $$("[data-computer]")) el.textContent = options.computer || "Mac";
  STARTERS.aloud.text = STARTERS.aloud.text.replace("this Mac", `this ${options.computer || "Mac"}`);
  systemBox.placeholder = options.default_system;
  systemBox.value = prefs.get("system", options.default_system);
  const languages = $("#tts-language");
  for (const name of options.tts_languages) languages.append(new Option(name, name));
  languages.value = prefs.get("ttsLanguage", "auto");
  if (!languages.value) languages.value = "auto";
  const row = (t) => {
    const li = el("li");
    li.append(el("b", null, TOOL_NAMES[t.name] || t.name), el("span", null, t.description));
    return li;
  };
  $("#tool-list-local").replaceChildren(...options.tools.filter((t) => t.group !== "web").map(row));
  $("#tool-list-web").replaceChildren(...options.tools.filter((t) => t.group === "web").map(row));
}

tempInput.value = prefs.get("temperature", 0);
maxTokensInput.value = prefs.get("maxTokens", 1024);
toolsSwitch.checked = prefs.get("tools", true);
webSwitch.checked = prefs.get("web", false);
const savedReply = prefs.get("reply", "auto");
for (const r of $$("input[name='reply']")) r.checked = r.value === savedReply;
for (const r of $$("input[name='tts-voice']")) r.checked = r.value === prefs.get("ttsVoice", "female");
$("#temp-out").textContent = tempInput.value;

function syncWeb() {
  webSwitch.disabled = liveWeb.disabled = !toolsSwitch.checked;
  liveWeb.checked = webSwitch.checked;
  $("#web-switch").title = toolsSwitch.checked
    ? "Lets it search the web and read pages in a separate, signed-out Chrome"
    : "Turn on Tools to use the web";
}
const webOn = () => toolsSwitch.checked && webSwitch.checked;

tempInput.addEventListener("input", () => { $("#temp-out").textContent = tempInput.value; prefs.set("temperature", Number(tempInput.value)); });
maxTokensInput.addEventListener("change", () => prefs.set("maxTokens", Number(maxTokensInput.value)));
systemBox.addEventListener("change", () => prefs.set("system", systemBox.value));
toolsSwitch.addEventListener("change", () => { prefs.set("tools", toolsSwitch.checked); syncWeb(); updateFootNote(); });
webSwitch.addEventListener("change", () => { prefs.set("web", webSwitch.checked); syncWeb(); updateFootNote(); });
liveWeb.addEventListener("change", () => { webSwitch.checked = liveWeb.checked; prefs.set("web", webSwitch.checked); updateFootNote(); });
for (const r of $$("input[name='reply']")) r.addEventListener("change", () => { prefs.set("reply", r.value); updateFootNote(); });
for (const r of $$("input[name='tts-voice']")) r.addEventListener("change", () => prefs.set("ttsVoice", r.value));
$("#tts-language").addEventListener("change", (e) => prefs.set("ttsLanguage", e.target.value));
$("#reset-system").addEventListener("click", () => { systemBox.value = options.default_system; prefs.set("system", systemBox.value); });

const replyMode = () => $("input[name='reply']:checked").value;
const ttsVoice = () => $("input[name='tts-voice']:checked").value;

async function loadReminders() {
  const list = $("#reminder-list");
  let data;
  try { data = await (await fetch("/api/reminders")).json(); } catch { return; }
  if (!data.reminders.length) {
    list.replaceChildren(el("li", "empty", "None yet. Try saying “Remind me to water the plants tomorrow.”"));
    $("#clear-reminders").hidden = true;
    return;
  }
  list.replaceChildren(...data.reminders.map((r) => {
    const li = el("li");
    li.append(el("span", null, r.text));
    if (r.when) li.append(el("small", null, r.when));
    return li;
  }));
  $("#clear-reminders").hidden = false;
}
$("#clear-reminders").addEventListener("click", async () => {
  await fetch("/api/reminders", { method: "DELETE" });
  loadReminders();
});

function openPanel(panel, button, open) {
  panel.hidden = !open;
  button.setAttribute("aria-expanded", String(open));
}
$("#open-settings").addEventListener("click", () => {
  const open = $("#settings").hidden;
  if (open && !side.hidden) showSide(null); // they share the right edge
  openPanel($("#settings"), $("#open-settings"), open);
  if (open) { loadReminders(); systemBox.focus(); }
});
$("#close-settings").addEventListener("click", () => openPanel($("#settings"), $("#open-settings"), false));

// ---------- side panel: the live conversation and the model log ----------
// One panel on the right, below the top bar. In live mode it has two tabs, Conversation and
// Log; in the chat it shows the log (the chat itself is the conversation).
const side = $("#side");
let sideTab = "log", logSince = 0, logTimer = null;
const REPLY_LINE = /^(Written|Spoken|Read-aloud) reply: /;

async function pullLogs() {
  try {
    const data = await (await fetch(`/api/logs?since=${logSince}`)).json();
    const body = $("#log-body");
    const atEnd = body.scrollTop + body.clientHeight >= body.scrollHeight - 30;
    for (const entry of data.lines) {
      const line = el("span", `src-${entry.src}`, entry.line);
      if (REPLY_LINE.test(entry.line)) line.classList.add("reply");
      if (/cut|limit|failed|more than the/.test(entry.line)) line.classList.add("trouble");
      body.append(line);
    }
    logSince = data.last;
    if (atEnd) body.scrollTop = body.scrollHeight;
  } catch {}
}

function placeSide() {
  const bar = live.open ? $("#live .live-top") : $(".top");
  side.style.top = `${Math.round(bar.getBoundingClientRect().bottom) + 6}px`;
}

// tab: "talk" | "log" to show that tab, or null to close the panel
function showSide(tab) {
  if (tab === "talk" && !live.open) tab = null;
  const open = Boolean(tab);
  if (open) sideTab = tab;
  const home = live.open ? $("#live") : document.body;
  if (side.parentElement !== home) home.append(side);
  side.hidden = !open;
  for (const t of $$("[role=tab]", side)) {
    const on = t.dataset.tab === sideTab;
    t.setAttribute("aria-selected", String(on));
    t.tabIndex = on ? 0 : -1;
  }
  $("#side-talk").hidden = sideTab !== "talk";
  $("#side-log").hidden = sideTab !== "log";
  const showing = (t) => String(open && sideTab === t);
  for (const id of ["#open-log", "#log-toggle", "#live-logs"]) $(id).setAttribute("aria-expanded", showing("log"));
  $("#live-talk").setAttribute("aria-expanded", showing("talk"));
  document.body.classList.toggle("side-open", open);
  $("#live").classList.toggle("with-side", open);
  clearInterval(logTimer);
  if (open && sideTab === "log") { pullLogs(); logTimer = setInterval(pullLogs, 1000); }
  if (open) {
    placeSide();
    const list = sideTab === "talk" ? $("#live-log") : $("#log-body");
    list.scrollTop = list.scrollHeight;
  }
  prefs.set(live.open ? "liveSide" : "side", open ? sideTab : "");
}
const toggleSide = (tab) => showSide(!side.hidden && sideTab === tab ? null : tab);

$("#open-log").addEventListener("click", () => toggleSide("log"));
$("#log-toggle").addEventListener("click", () => {
  openPanel($("#settings"), $("#open-settings"), false);
  toggleSide("log");
});
$("#side-close").addEventListener("click", () => showSide(null));
for (const t of $$("[role=tab]", side)) t.addEventListener("click", () => showSide(t.dataset.tab));
$(".side-tabs").addEventListener("keydown", (e) => {
  if (!["ArrowLeft", "ArrowRight"].includes(e.key) || !live.open) return;
  const next = sideTab === "talk" ? "log" : "talk";
  showSide(next);
  $(`#tab-${next}`).focus();
});
window.addEventListener("resize", () => { if (!side.hidden) placeSide(); });

// ---------- attachments ----------
const IMAGE_EXT = /\.(png|jpe?g|gif|webp|bmp|tiff?|heic|heif)$/i;
const AUDIO_EXT = /\.(wav|mp3|m4a|aac|ogg|oga|opus|flac|aiff?|caf|weba)$/i;
const VIDEO_EXT = /\.(mp4|mov|m4v|mkv|avi|webm|mpe?g|3gp)$/i;
function guessKind(file) {
  if (file.type.startsWith("image/") || IMAGE_EXT.test(file.name)) return "image";
  if (file.type.startsWith("audio/") || AUDIO_EXT.test(file.name)) return "audio";
  if (file.type.startsWith("video/") || VIDEO_EXT.test(file.name)) return "video";
  return null;
}

const mediaInfo = new Map(); // server media id -> {kind, url, name, voice, seconds, soundtrack}
const transcriptSlots = new Map(); // media id -> the <p> under a voice message that shows what was said
let staged = [];

function makeItem(file, voice = false) {
  const item = { file, voice, kind: guessKind(file), name: file.name, url: URL.createObjectURL(file), info: null, error: null };
  const body = new FormData();
  body.append("file", file, file.name);
  if (voice) body.append("voice", "1");
  item.promise = fetch("/api/media", { method: "POST", body })
    .then(async (r) => {
      if (!r.ok) throw new Error(await errorFrom(r));
      item.info = await r.json();
      item.kind = item.info.kind;
      mediaInfo.set(item.info.id, { ...item.info, url: item.url });
    })
    .catch((err) => { item.error = err.message.startsWith("Failed to fetch") ? "The app server isn't responding." : err.message; })
    .finally(() => { renderStaged(); live.renderTray(); });
  return item;
}

function stage(files) {
  for (const file of files) {
    if (!guessKind(file)) { flashNote(`${file.name} isn't an image, audio recording or video.`); continue; }
    staged.push(makeItem(file));
  }
  renderStaged();
}

function thumb(kind, url) {
  if (kind === "image") {
    const img = el("img");
    img.src = url;
    img.alt = "";
    img.onerror = () => img.replaceWith(el("span", "thumb-word", "Photo"));
    return img;
  }
  if (kind === "video") {
    const v = el("video");
    v.src = url;
    v.muted = true;
    v.preload = "metadata";
    return v;
  }
  return el("span", "thumb-word", "Audio");
}

function itemDetail(item) {
  if (item.error) return item.error;
  if (!item.info) return "Preparing…";
  if (item.info.kind === "audio") return `${fmtClock(item.info.seconds)} of audio`;
  if (item.info.kind === "video") return item.info.soundtrack ? "Video with sound" : "Video, no sound";
  return "Image";
}

// one attachment waiting to be sent: thumbnail, name, state, and a button that drops it
function chipFor(item, onRemove, detail = itemDetail(item)) {
  const chip = el("div", `attachment${item.error ? " failed" : ""}${item.info ? "" : " pending"}`);
  const t = el("span", "thumb");
  t.append(thumb(item.kind, item.url));
  const text = el("span", "attachment-text");
  text.append(el("span", "attachment-name", item.name), el("span", "attachment-detail", detail));
  const remove = el("button", "remove", "×");
  remove.type = "button";
  remove.title = `Remove ${item.name}`;
  remove.setAttribute("aria-label", `Remove ${item.name}`);
  remove.addEventListener("click", onRemove);
  chip.append(t, text, remove);
  return chip;
}

function renderStaged() {
  const box = $("#staged");
  box.hidden = !staged.length;
  box.replaceChildren(...staged.map((item) => chipFor(item, () => {
    staged = staged.filter((s) => s !== item);
    renderStaged();
    $("#input").focus();
  })));
  renderSuggestions();
}

const SUGGESTIONS = {
  image: ["Describe this image.", "Read all the text in it.", "What stands out here?"],
  audio: ["Transcribe this word for word.", "Summarize what's said.", "Translate it into English."],
  video: ["Describe what happens in this clip.", "What can you hear in it?", "Summarize this video in one sentence."],
};
function renderSuggestions() {
  const box = $("#suggestions");
  const kinds = [...new Set(staged.filter((s) => !s.error).map((s) => s.kind))];
  const ideas = kinds.length === 1 ? SUGGESTIONS[kinds[0]] : kinds.length > 1 ? ["Compare these.", "How are these related?"] : [];
  box.hidden = !ideas.length || $("#input").value.trim() !== "";
  box.replaceChildren(...ideas.map((idea) => {
    const chip = el("button", "chip", idea);
    chip.type = "button";
    chip.addEventListener("click", () => send({ text: idea }));
    return chip;
  }));
}

$("#file-input").addEventListener("change", (e) => {
  stage([...e.target.files]);
  e.target.value = "";
  $("#input").focus();
});

let dragDepth = 0;
document.addEventListener("dragenter", (e) => {
  if (!e.dataTransfer?.types.includes("Files")) return;
  dragDepth++;
  document.body.classList.add("dragging");
});
document.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) document.body.classList.remove("dragging");
});
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove("dragging");
  if (!e.dataTransfer.files.length) return;
  if (live.open) live.attach([...e.dataTransfer.files]);
  else stage([...e.dataTransfer.files]);
});
$("#input").addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); stage(files); }
});
document.addEventListener("paste", (e) => { // in live mode the message box is out of reach
  const files = [...(e.clipboardData?.files || [])];
  if (live.open && files.length) { e.preventDefault(); live.attach(files); }
});

// ---------- the composer ----------
const input = $("#input");
function fitInput() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, window.innerHeight * 0.35)}px`;
}
input.addEventListener("input", () => { fitInput(); renderSuggestions(); });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send();
  }
});
$("#composer").addEventListener("submit", (e) => { e.preventDefault(); send(); });

let noteTimer = null, noteWaitsForModel = false;
function flashNote(text, waitsForModel = false) {
  const note = $("#foot-note");
  noteWaitsForModel = waitsForModel;
  note.textContent = text;
  note.classList.add("alert");
  clearTimeout(noteTimer);
  noteTimer = setTimeout(updateFootNote, 6000);
}
function updateFootNote() {
  const note = $("#foot-note");
  noteWaitsForModel = false;
  note.classList.remove("alert");
  const mode = {
    auto: "Speaks back to voice, writes back to text.",
    text: "Always answers in writing.",
    voice: "Always answers out loud.",
  }[replyMode()];
  note.textContent = webOn() ? `${mode} It can use the web.` : mode;
}

let current = null; // the reply being generated: {rid, stopRequested}
let sending = false;

function refreshControls() {
  const busy = Boolean(current) || sending;
  $("#send").hidden = busy;
  $("#stop-reply").hidden = !(busy || player.active);
  $("#send").disabled = !isReady();
  $("#record").disabled = (!isReady() && !recorder) || busy;
  for (const b of $$(".starter, .sample, .chip")) b.disabled = !isReady() || busy;
  for (const b of $$(".live-open")) {
    b.disabled = !isReady() || busy || Boolean(recorder);
    b.title = isReady() ? "Talk with it hands-free" : "Start the model first";
  }
  for (const b of $$(".speak-btn")) b.disabled = !isReady() || busy || b.dataset.running === "1";
  for (const b of $$(".retry")) b.disabled = !isReady() || busy;
  live.refresh?.();
}

// A tool at work shows a small Thinking Orb in its icon, in the colour of its kind:
// blue for the tools on this Mac, amber for the web. Without a tool, the icon goes back
// to a tick or a cross.
const INK_RGB = { local: [47, 91, 211], web: [138, 83, 0] };
function workingOrb(icon, tool, web) {
  icon.querySelector(".verb-orb")?.remove();
  icon.classList.toggle("has-orb", !!tool);
  if (tool) icon.append(verbOrb(verbFor(tool), 22, INK_RGB[web ? "web" : "local"]));
}

// ---------- the orb in the message bar, and the glow around the bar ----------
// The orb shows what the model is doing: a knot while it thinks, rings while it searches
// the web, a sphere pulsing with the voice while it speaks. Clicking it opens live mode.
// It only animates while something is happening, or while the pointer is on it.
const barOrb = new SpeakingOrb($("#bar-orb"), { particles: 380, dots: 2.4 });
const welcomeOrb = new SpeakingOrb($("#welcome-orb"), { particles: 2600, dots: 1.3 });
const ACTIVITY_ORB = { thinking: "thinking", searching: "searching", speaking: "speaking", recording: "listening" };
let idleTimer = null;
function idleSoon() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => { if (!document.body.dataset.activity || live.open) barOrb.pause(); }, 1500);
}
function setActivity(activity) {
  document.body.dataset.activity = activity;
  barOrb.state = ACTIVITY_ORB[activity] || "listening";
  if (activity !== "speaking") barOrb.endLine();
  if (activity && !live.open) { clearTimeout(idleTimer); barOrb.start(); }
  else idleSoon();
}
function barSpeaking(clock) {
  barOrb.analyser = player.analyser;
  barOrb.beginLine(clock);
  setActivity("speaking");
}
$("#live-button").addEventListener("pointerenter", () => { if (!live.open) { clearTimeout(idleTimer); barOrb.start(); } });
$("#live-button").addEventListener("pointerleave", () => { if (!document.body.dataset.activity) idleSoon(); });

// ---------- the conversation ----------
let history = []; // model-ready turns, appended from each reply's `done.append`
const thread = $("#thread");

function nearBottom() {
  return window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 160;
}
function keepInView(wasNear) {
  if (wasNear) window.scrollTo({ top: document.documentElement.scrollHeight });
}

function showWelcome() {
  const show = thread.children.length === 0;
  $("#welcome").hidden = !show;
  if (show && !live.open) welcomeOrb.start();
  else welcomeOrb.pause();
}

// Each turn is a row of the transcript: who in the margin, what they said in the column.
function turnShell(kind, who) {
  const li = el("li", `turn ${kind}`);
  const margin = el("div", "who");
  margin.append(el("b", null, who));
  const body = el("div", "body");
  li.append(margin, body);
  return { li, margin, body };
}

function renderUserTurn(text, ids) {
  const { li, body } = turnShell("user", "You");
  for (const id of ids) {
    const m = mediaInfo.get(id);
    // photos and videos sit on dark glass, like the pages the browser saw; recordings stay light
    const box = el("figure", `sent-media ${m.kind}${m.kind === "audio" ? "" : " media-card"}`);
    if (m.kind === "image") {
      box.append(thumb("image", m.url));
    } else if (m.kind === "video") {
      const v = el("video");
      v.src = m.url;
      v.controls = true;
      v.preload = "metadata";
      box.append(v);
    } else {
      const a = el("audio");
      a.src = m.url;
      a.controls = true;
      a.preload = "metadata";
      box.append(a);
    }
    box.append(el("figcaption", null, m.voice ? `Voice message, ${fmtClock(m.seconds)}` : m.name));
    if (m.voice) {
      const said = el("p", "transcript");
      said.hidden = true;
      said.dir = "auto";
      box.append(said);
      transcriptSlots.set(id, said);
    }
    body.append(box);
  }
  if (text) {
    const p = el("p", "user-text", text);
    p.dir = "auto";
    body.append(p);
  }
  thread.append(li);
  showWelcome();
  return li;
}

function showTranscript(id, text) {
  const slot = transcriptSlots.get(id);
  if (!slot) return;
  slot.textContent = `“${text}”`;
  slot.hidden = false;
}

// the caption on a screenshot card: what was searched or which page, and where from
function shotCaption(name, result) {
  if (name === "web_search") {
    const n = result?.results?.length || 0;
    return [`“${result?.query ?? ""}”`, result?.error || `${n} result${n === 1 ? "" : "s"} from ${result?.engine ?? "the web"}`];
  }
  return [result?.title || "Untitled page", result?.error || hostOf(result?.url || "")];
}

function toolDetail(name, args, result) {
  if (!result) {
    if (name === "web_search") return `“${args?.query ?? ""}”`;
    if (name === "open_page") return String(args?.url ?? "");
    if (name === "click") return `element ${args?.number ?? ""}`;
    if (name === "type_text") return `“${args?.text ?? ""}” into ${args?.number ?? ""}${args?.submit === true || args?.submit === "true" ? ", then Enter" : ""}`;
    if (name === "go_back") return "the previous page";
    return "Running…";
  }
  if (result.error) return result.error;
  if (name === "get_current_time") return `${result.time} on ${result.date}, ${result.timezone}`;
  if (name === "calculate") return `${result.expression} = ${result.result}`;
  if (name === "add_reminder") return `“${result.text}”${result.when ? `, ${result.when}` : ""}`;
  if (name === "list_reminders") return result.count === 1 ? "1 reminder saved" : `${result.count} reminders saved`;
  if (name === "web_search") {
    const n = result.results?.length || 0;
    return `“${result.query}”, ${n} result${n === 1 ? "" : "s"} from ${result.engine}`;
  }
  if (name === "click" && result.clicked) return `${result.clicked} → ${result.title || "Untitled page"}, ${hostOf(result.url)}`;
  if (name === "type_text" && result.typed_into) return `“${args?.text ?? ""}” into the ${result.typed_into} → ${result.title || "Untitled page"}`;
  if (["open_page", "click", "type_text", "go_back"].includes(name)) return `${result.title || "Untitled page"}, ${hostOf(result.url)}`;
  return JSON.stringify(result);
}

class ReplyView {
  constructor() {
    const { li, margin, body } = turnShell("model", "LFM3");
    this.li = li;
    this.margin = margin;
    this.parts = el("div", "parts");
    this.waiting = el("p", "waiting", "Thinking…");
    this.foot = el("div", "turn-foot");
    body.append(this.parts, this.waiting, this.foot);
    thread.append(li);
    showWelcome();
    this.texts = [];
    this.newSegment();
  }
  newSegment() {
    this.segText = "";
    this.seg = el("div", "reading");
    this.seg.dir = "auto";
    this.parts.append(this.seg);
  }
  start(speak) {
    this.speak = speak;
    this.waiting.textContent = speak ? "Preparing a spoken reply…" : "Thinking…";
  }
  speaking() {
    this.waiting.textContent = "Speaking…";
  }
  token(text) {
    const near = nearBottom();
    this.segText += text;
    if (!this.speak) this.waiting.hidden = true;
    if (!this.frame) {
      this.frame = requestAnimationFrame(() => {
        this.frame = null;
        this.seg.innerHTML = md(this.segText) || "<p></p>";
        (this.seg.lastElementChild || this.seg).append(el("span", "caret"));
        keepInView(near);
      });
    }
  }
  settleSegment(text) {
    cancelAnimationFrame(this.frame);
    this.frame = null;
    this.segText = text;
    if (text.trim()) {
      this.seg.innerHTML = md(text);
      this.texts.push(text.trim());
    } else {
      this.seg.remove();
    }
  }
  toolCall({ name, arguments: args }) {
    const near = nearBottom();
    this.settleSegment(this.segText);
    this.card = el("div", `tool pending${WEB_TOOLS.has(name) ? " web" : ""}`);
    const details = el("details");
    const summary = el("summary");
    const icon = el("span", "tool-icon");
    workingOrb(icon, name, WEB_TOOLS.has(name));
    summary.append(icon, el("b", null, TOOL_PENDING[name] || name), el("span", "tool-line", toolDetail(name, args, null)));
    const raw = el("pre", "tool-raw", `${name}(${JSON.stringify(args)})`);
    details.append(summary, raw);
    this.card.append(details);
    this.parts.append(this.card);
    this.waiting.hidden = false;
    this.waiting.textContent = WEB_TOOLS.has(name) ? "Waiting for the page…" : "Running the tool…";
    keepInView(near);
  }
  toolResult({ name, result, screenshot }) {
    const card = this.card;
    card.classList.remove("pending");
    workingOrb($(".tool-icon", card), null);
    if (result?.error) card.classList.add("failed");
    $("summary b", card).textContent = TOOL_LABELS[name] || name;
    $(".tool-line", card).textContent = toolDetail(name, null, result);
    $(".tool-raw", card).textContent += `\n→ ${JSON.stringify(result, null, 1)}`;
    if (screenshot) {
      const link = el("a", "shot media-card");
      link.href = screenshot;
      link.target = "_blank";
      link.rel = "noopener";
      link.title = "What the browser showed. Opens full size.";
      const img = el("img");
      img.src = screenshot;
      img.alt = `Screenshot: ${toolDetail(name, null, result)}`;
      const [title, source] = shotCaption(name, result);
      const caption = el("span", "shot-caption");
      caption.append(el("b", null, title), el("small", null, source));
      link.append(img, caption);
      card.append(link);
    }
    this.waiting.textContent = this.speak ? "Preparing a spoken reply…" : "Thinking…";
    this.newSegment();
  }
  done(event) {
    const near = nearBottom();
    this.waiting.hidden = true;
    if (event.audio) {
      const audio = el("audio", "spoken");
      audio.controls = true;
      audio.preload = "metadata";
      audio.src = `/outputs/${event.audio}`;
      audio.title = "Play the spoken reply again";
      this.parts.insertBefore(audio, this.seg); // after any tool cards, above its words
    }
    this.settleSegment(event.text || "");
    if (!this.texts.length && !event.audio) {
      const empty = el("p", "note", event.stats?.finish === "cancelled" ? "Stopped before it said anything." : "The model returned an empty reply.");
      this.parts.append(empty);
    }
    for (const line of statsLines(event.stats)) this.margin.append(el("small", null, line));
    const words = this.texts.join("\n");
    if (words) {
      const speak = el("button", "btn quiet small speak-btn", "Read aloud");
      speak.type = "button";
      speak.addEventListener("click", () => readAloud(this, speak, words));
      const copy = el("button", "btn quiet small", "Copy");
      copy.type = "button";
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(words); copy.textContent = "Copied"; }
        catch { copy.textContent = "Couldn't copy"; }
        setTimeout(() => (copy.textContent = "Copy"), 1500);
      });
      this.foot.replaceChildren(speak, copy);
    }
    refreshControls();
    keepInView(near);
  }
  note(text) {
    this.margin.append(el("small", "trim-note", text));
  }

  fail(message, retry) {
    this.waiting.hidden = true;
    this.settleSegment(this.segText);
    this.parts.append(el("p", "error-note", message));
    this.li.previousElementSibling?.classList.add("unsent");
    if (retry) {
      const again = el("button", "btn small retry", "Try again");
      again.type = "button";
      again.addEventListener("click", () => {
        this.li.previousElementSibling?.classList.remove("unsent");
        this.li.remove();
        retry();
      });
      this.margin.append(el("small", null, "This message isn't part of the conversation yet."));
      this.foot.replaceChildren(again);
    }
    refreshControls();
  }
}

async function readAloud(view, button, text) {
  player.stop();
  button.dataset.running = "1";
  button.textContent = "Reading…";
  refreshControls();
  try {
    const r = await fetch("/api/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, language: $("#tts-language").value, voice: ttsVoice() }),
    });
    if (!r.ok) throw new Error(await errorFrom(r));
    const data = await r.json();
    let audio = $("audio.read-aloud", view.li);
    if (!audio) {
      audio = el("audio", "read-aloud");
      audio.controls = true;
      view.foot.before(audio);
    }
    audio.src = `/outputs/${data.audio}`;
    audio.title = `${data.language}, ${ttsVoice()} voice`;
    audio.play().catch(() => {});
  } catch (err) {
    flashNote(err.message || "Couldn't read that aloud.");
  } finally {
    button.dataset.running = "";
    button.textContent = "Read aloud";
    pollStatus();
    refreshControls();
  }
}

// options: text, items, reply, clear, tools, web (per-message overrides), hooks (live mode)
async function send({ text, items, reply, clear, tools, web, hooks } = {}) {
  if (current || sending) return false;
  player.prime(); // still inside the click or key press, so the reply may play sound
  clear = clear ?? items === undefined; // composer sends clear the box; starters leave it alone
  text = (text ?? input.value).trim();
  items = items ?? staged.slice();
  if (!text && !items.length) return false;
  if (!isReady()) { flashNote("Start the model first. The button is at the top of the page.", true); return false; }
  sending = true;
  refreshControls();
  await Promise.all(items.map((i) => i.promise));
  sending = false;
  const failed = items.find((i) => i.error);
  if (failed) {
    flashNote(`Remove ${failed.name} first: ${failed.error}`);
    hooks?.fail(failed.error);
    refreshControls();
    return false;
  }
  if (clear) {
    input.value = "";
    fitInput();
    staged = staged.filter((s) => !items.includes(s));
    renderStaged();
  }
  for (const b of $$(".retry")) b.remove(); // a retry would land after this new turn
  const message = { text, media: items.map((i) => i.info.id) };
  renderUserTurn(text, message.media);
  runTurn(message, reply || replyMode(), {
    tools: tools ?? toolsSwitch.checked,
    web: web ?? webOn(),
    hooks,
  });
  return true;
}

async function runTurn(message, reply, opts) {
  const { hooks } = opts;
  const view = new ReplyView();
  window.scrollTo({ top: document.documentElement.scrollHeight });
  player.stop();
  current = { rid: null, stopRequested: false };
  refreshControls();
  setActivity("thinking");
  const retry = () => runTurn(message, reply, opts);
  const body = {
    history, message, reply,
    tools: opts.tools,
    web: opts.web,
    system: systemBox.value.trim() === options.default_system ? "" : systemBox.value.trim(),
    temperature: Number(tempInput.value),
    max_tokens: Number(maxTokensInput.value) || 1024,
  };
  let session = null; // this reply's turn on the player
  const ended = (natural) => { if (!current) setActivity(""); refreshControls(); hooks?.played(natural); };
  try {
    const response = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!response.ok) {
      const message = await errorFrom(response);
      current = null;
      view.fail(message, retry);
      hooks?.fail(message);
      return;
    }
    let finished = false;
    for await (const event of ndjson(response)) {
      const kind = event.event;
      if (kind === "started") {
        current.rid = event.id;
        view.start(event.speak);
        hooks?.started();
        pollStatus();
        if (current.stopRequested) stopReply();
      } else if (kind === "token") {
        view.token(event.text);
        hooks?.token(event.text);
      } else if (kind === "audio") {
        if (session === null) {
          if (current?.stopRequested) continue;
          session = player.begin(ended);
          view.speaking();
          barSpeaking(() => player.heard());
          hooks?.speaking(() => player.heard());
          refreshControls();
        }
        player.push(session, event.pcm, event.rate);
      } else if (kind === "tool_call") {
        setActivity(WEB_TOOLS.has(event.name) ? "searching" : "thinking");
        view.toolCall(event);
        hooks?.toolCall(event);
      } else if (kind === "tool_result") {
        setActivity("thinking");
        view.toolResult(event);
        hooks?.toolResult(event);
        if (event.name === "add_reminder" && !$("#settings").hidden) loadReminders();
      } else if (kind === "done") {
        finished = true;
        history.push(...event.append);
        const cancelled = event.stats?.finish === "cancelled";
        current = null;
        view.done(event);
        if (session !== null) player.finish(session);
        else if (event.audio && !cancelled) {
          // nothing was streamed (an older worker, say): play the saved file instead
          player.playUrl(`/outputs/${event.audio}`, ended);
          barSpeaking(() => player.heard());
          hooks?.speaking(() => player.heard());
        }
        hooks?.done(event, session !== null || (Boolean(event.audio) && !cancelled));
        refreshControls();
      } else if (kind === "trimmed") {
        view.note(`Left out the ${event.left_out} earliest messages: with them the prompt was ${event.prompt_tokens.toLocaleString()} tokens, more than the model's 32K context.`);
        hooks?.trimmed(event);
      } else if (kind === "transcript") {
        showTranscript(event.media, event.text);
        hooks?.transcript(event);
      } else if (kind === "error") {
        finished = true;
        current = null;
        if (session !== null) player.stop();
        view.fail(event.message, retry);
        hooks?.fail(event.message);
      }
    }
    if (!finished) {
      current = null;
      view.fail("The connection dropped before the answer finished.", retry);
      hooks?.fail("The connection dropped.");
    }
  } catch {
    current = null;
    view.fail("The app server isn't responding. Check that start.sh is still running.", retry);
    hooks?.fail("The app server isn't responding.");
  } finally {
    current = null;
    if (!player.active) setActivity("");
    refreshControls();
    pollStatus();
  }
}

async function stopReply() {
  player.stop();
  if (sending || !current) { refreshControls(); return; }
  if (!current.rid) { current.stopRequested = true; return; }
  $("#stop-reply").disabled = true;
  try { await fetch(`/api/cancel/${current.rid}`, { method: "POST" }); } catch {}
  $("#stop-reply").disabled = false;
}
$("#stop-reply").addEventListener("click", stopReply);

async function waitIdle(limit = 15000) {
  const until = Date.now() + limit;
  while ((current || sending) && Date.now() < until) await sleep(50);
  return !current && !sending;
}

$("#new-chat").addEventListener("click", async () => {
  if (current) await stopReply();
  player.stop();
  history = [];
  staged = [];
  transcriptSlots.clear();
  thread.replaceChildren();
  renderStaged();
  input.value = "";
  fitInput();
  showWelcome();
  window.scrollTo({ top: 0 });
  input.focus();
});

// ---------- voice messages ----------
class Recorder {
  async start() {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error("This browser can't record audio here.");
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const type = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"].find((t) => MediaRecorder.isTypeSupported(t));
    this.recorder = new MediaRecorder(this.stream, type ? { mimeType: type } : {});
    this.chunks = [];
    this.recorder.ondataavailable = (e) => e.data.size && this.chunks.push(e.data);
    this.recorder.start();
    this.startedAt = Date.now();
  }
  stop() {
    return new Promise((resolve) => {
      this.recorder.onstop = () => {
        this.stream.getTracks().forEach((t) => t.stop());
        const type = this.recorder.mimeType || "audio/webm";
        const ext = type.includes("mp4") ? "m4a" : "webm";
        resolve(new File(this.chunks, `voice-message.${ext}`, { type }));
      };
      this.recorder.stop();
    });
  }
  get seconds() { return (Date.now() - this.startedAt) / 1000; }
}

function micErrorText(err) {
  if (err?.name === "NotAllowedError") return "Microphone access is blocked. Allow it for this page in the browser's site settings.";
  if (err?.name === "NotFoundError") return "No microphone was found.";
  return err?.message || "Couldn't start the microphone.";
}

let recorder = null, recordTimer = null, askingMic = false;
const recordButton = $("#record");
const recordLabel = $("#record-label");

recordButton.addEventListener("click", async () => {
  if (askingMic) return; // the browser is still asking for microphone access
  player.prime();
  if (!recorder) {
    askingMic = true;
    recordButton.classList.add("asking");
    recordLabel.textContent = "Allow the microphone…";
    try {
      const next = new Recorder();
      await next.start();
      recorder = next;
    } catch (err) {
      recordLabel.textContent = "Record";
      flashNote(micErrorText(err));
      return;
    } finally {
      askingMic = false;
      recordButton.classList.remove("asking");
    }
    player.stop();
    setActivity("recording");
    recordButton.classList.add("on");
    recordButton.setAttribute("aria-pressed", "true");
    recordLabel.textContent = "Send 0:00";
    recordButton.title = "Stop recording and send it";
    recordTimer = setInterval(() => (recordLabel.textContent = `Send ${fmtClock(recorder.seconds)}`), 250);
    refreshControls();
    return;
  }
  clearInterval(recordTimer);
  const seconds = recorder.seconds;
  const file = await recorder.stop();
  recorder = null;
  setActivity("");
  recordButton.classList.remove("on");
  recordButton.setAttribute("aria-pressed", "false");
  recordButton.title = "Record a voice message";
  recordLabel.textContent = "Record";
  refreshControls();
  if (seconds < 0.6) { flashNote("That recording was too short. Click Record, speak, then click Send."); return; }
  const voice = makeItem(file, true);
  // A voice message goes straight out, along with anything already typed or attached.
  send({ text: input.value, items: [...staged, voice], clear: true });
});

// ---------- live conversation ----------
// The microphone stays open. A small voice detector (an AudioWorklet that hands over 20 ms
// frames) waits for speech, keeps 300 ms from before it started, and ends the turn after
// 0.8 s of quiet. The turn goes out as a voice message with a spoken reply, through the
// same path as everything else, so it lands in the conversation too. While the model
// thinks and speaks the detector ignores the microphone, unless "talk over it" is on.
const TAP = `class Tap extends AudioWorkletProcessor {
  constructor() { super(); this.buf = new Float32Array(Math.round(sampleRate * 0.02)); this.n = 0; }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) for (let i = 0; i < ch.length; i++) {
      this.buf[this.n++] = ch[i];
      if (this.n === this.buf.length) { this.port.postMessage(this.buf.slice()); this.n = 0; }
    }
    return true;
  }
}
registerProcessor("tap", Tap);`;

const FRAME = 0.02;
const VAD = {
  startFrames: 4,      // 80 ms above the start level begins a turn
  endFrames: 40,       // 0.8 s below the end level ends it
  preFrames: 15,       // 300 ms kept from before the start
  minVoiced: 0.35,     // seconds of actual speech, or it was a cough
  maxSeconds: 30,
  bargeFrames: 8,      // 160 ms of loud speech to cut in while it talks
};

function wavFile(chunks, rate) {
  const n = chunks.reduce((s, c) => s + c.length, 0);
  const view = new DataView(new ArrayBuffer(44 + n * 2));
  const put = (at, s) => { for (let i = 0; i < s.length; i++) view.setUint8(at + i, s.charCodeAt(i)); };
  put(0, "RIFF"); view.setUint32(4, 36 + n * 2, true); put(8, "WAVE");
  put(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  put(36, "data"); view.setUint32(40, n * 2, true);
  let at = 44;
  for (const c of chunks) for (let i = 0; i < c.length; i++, at += 2) view.setInt16(at, clamp(c[i], -1, 1) * 32767, true);
  return new File([view.buffer], "live-turn.wav", { type: "audio/wav" });
}

const PHASE_TEXT = {
  listening: "Listening",
  hearing: "Hearing you",
  sending: "Got it",
  thinking: "Thinking",
  searching: "Searching the web",
  reading: "Reading a page",
  speaking: "Speaking",
  muted: "Muted. Unmute to talk.",
};
const ORB_STATE = { listening: "listening", hearing: "hearing", muted: "muted", sending: "gathering", thinking: "thinking", searching: "searching", reading: "reading", speaking: "speaking", done: "done", error: "error" };

// ---------- live mode: how each reply went ----------
const PERF_TOOLS = {
  get_current_time: "Clock", calculate: "Calculator", add_reminder: "Reminder", list_reminders: "Reminders",
  web_search: "Web search", open_page: "Open page", click: "Click", type_text: "Type", go_back: "Go back",
};
const SVG = "http://www.w3.org/2000/svg";
const svgEl = (tag, attrs) => {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
};

const perf = {
  answers: [], // seconds from the end of your words to the first sound, oldest first
  turn: null,
  leftOut: 0,
  reset() { this.answers = []; this.turn = null; this.render(null); },
  begin(at) { this.turn = { at, tools: [], answered: null }; this.leftOut = 0; },
  toolCall(name) { if (this.turn) this.turn.toolAt = [name, performance.now()]; },
  toolResult() {
    const t = this.turn;
    if (!t?.toolAt) return;
    t.tools.push([t.toolAt[0], (performance.now() - t.toolAt[1]) / 1000]);
    t.toolAt = null;
  },
  firstSound() {
    const t = this.turn;
    if (t && t.answered === null) t.answered = (performance.now() - t.at) / 1000;
  },
  done(stats) {
    if (!this.turn) return;
    if (this.turn.answered !== null) this.answers = [...this.answers, this.turn.answered].slice(-12);
    this.render(stats);
    this.turn = null;
  },
  failed() { this.turn = null; },

  render(stats) {
    const list = $("#perf-list");
    $("#perf-empty").hidden = Boolean(stats);
    list.hidden = !stats;
    if (!stats) return;
    const t = this.turn || {};
    const tile = (label, value, note, cls) => {
      const d = el("div", cls);
      const dd = el("dd", null, value);
      if (note) dd.append(el("small", null, note));
      d.append(el("dt", null, label), dd);
      return d;
    };
    const tiles = [];
    if (t.answered != null) {
      const d = tile("Answered in", `${t.answered.toFixed(1)} s`, null, "answer");
      d.title = "From the end of your words to its first sound";
      const spark = this.spark();
      if (spark) $("dd", d).append(spark);
      tiles.push(d);
    }
    const made = stats.generate_seconds || stats.seconds;
    if (stats.audio_seconds) {
      // speech made per second once it started playing; under 1x, playback catches up and stutters
      const pace = made ? stats.audio_seconds / Math.max(0.1, made - (stats.first_audio_seconds || 0)) : 0;
      const slow = pace && pace < 1.15 && stats.audio_seconds > 3;
      const d = tile("Speech", `${stats.audio_seconds.toFixed(1)} s`, pace ? `${pace.toFixed(1)}× real time` : null, slow ? "warn" : "");
      d.title = slow ? "Made barely faster than it plays, so playback can stutter" : "How much faster than it plays the speech was made";
      tiles.push(d);
    }
    if (stats.tokens_per_sec) {
      const d = tile("Speed", `${Math.round(stats.tokens_per_sec)} tokens/s`);
      d.title = `${stats.tokens.toLocaleString()} tokens in ${made.toFixed(1)} s`;
      tiles.push(d);
    }
    for (const [name, secs] of t.tools || []) tiles.push(tile(PERF_TOOLS[name] || name, `${secs.toFixed(1)} s`));
    if (stats.prompt_tokens) {
      const max = stats.context_tokens || 32768, share = stats.prompt_tokens / max;
      const level = share > 0.9 ? "bad" : share > 0.7 ? "warn" : "";
      const d = tile("Context", `${stats.prompt_tokens.toLocaleString()} of ${Math.round(max / 1024)}K`, null, level);
      d.title = "Prompt tokens out of the model's context, which holds the prompt and the reply together";
      const meter = el("span", "meter");
      meter.setAttribute("role", "meter");
      meter.setAttribute("aria-label", "Context used");
      meter.setAttribute("aria-valuemin", "0");
      meter.setAttribute("aria-valuemax", String(max));
      meter.setAttribute("aria-valuenow", String(stats.prompt_tokens));
      meter.style.setProperty("--w", `${Math.min(100, share * 100).toFixed(1)}%`);
      if (level) meter.style.setProperty("--m", level === "bad" ? "var(--fault)" : "var(--amber)");
      meter.append(el("i"));
      $("dd", d).append(meter);
      tiles.push(d);
    }
    if (this.leftOut) tiles.push(tile("Left out", `${this.leftOut} messages`, "to fit", "warn"));
    if (stats.peak_memory_gb) tiles.push(tile("Memory", `${stats.peak_memory_gb.toFixed(1)} GB`, "peak"));
    if (FINISH_NOTES[stats.finish] && stats.finish !== "cancelled") tiles.push(tile("Ended", FINISH_NOTES[stats.finish], null, "bad"));
    list.replaceChildren(...tiles);
  },

  // answer times across the conversation: a small line, the latest point in the accent
  spark() {
    const n = this.answers.length;
    if (n < 2) return null;
    const w = 72, h = 22, pad = 3, top = Math.max(...this.answers);
    const x = (i) => pad + (i * (w - 2 * pad)) / (n - 1);
    const y = (v) => h - pad - (v / top) * (h - 2 * pad);
    const svg = svgEl("svg", { class: "spark", viewBox: `0 0 ${w} ${h}`, role: "img",
      "aria-label": `Answer times so far, oldest first: ${this.answers.map((v) => `${v.toFixed(1)} s`).join(", ")}` });
    svg.append(svgEl("polyline", { points: this.answers.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ") }));
    this.answers.forEach((v, i) => {
      const last = i === n - 1;
      svg.append(svgEl("circle", { class: last ? "dot now" : "dot", cx: x(i), cy: y(v), r: last ? 3 : 2 }));
      const hit = svgEl("circle", { class: "hit", cx: x(i), cy: y(v), r: 7 });
      const title = svgEl("title", {});
      title.textContent = `Reply ${i + 1}: answered in ${v.toFixed(1)} s`;
      hit.append(title);
      svg.append(hit);
    });
    return svg;
  },
};

function showPerf(open) {
  $("#live-perf").hidden = !open;
  $("#live-stats").setAttribute("aria-pressed", String(open));
  prefs.set("livePerf", open);
}

// "Voice, " or "Photo and voice, " or "2 photos, video and voice, ": the start of a live "You" entry
const KIND_WORDS = { image: ["photo", "photos"], video: ["video", "videos"], audio: ["recording", "recordings"] };
function shownWords(items) {
  const counts = {};
  for (const i of items) counts[i.kind] = (counts[i.kind] || 0) + 1;
  const parts = [...Object.entries(counts).map(([k, n]) => (n === 1 ? KIND_WORDS[k][0] : `${n} ${KIND_WORDS[k][1]}`)), "voice"];
  const text = parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(", ")} and ${parts.at(-1)}`;
  return `${text[0].toUpperCase()}${text.slice(1)}, `;
}

const live = {
  open: false,
  phase: "idle",
  muted: false,
  level: 0,
  pitch: 0.4,
  floor: 0.004,
  pace: 1,
  orb: null,

  async start() {
    if (this.open) return;
    if (!isReady()) { flashNote("Start the model first. The button is at the top of the page.", true); return; }
    player.prime();
    player.stop();
    this.open = true;
    this.muted = false;
    barOrb.pause();
    welcomeOrb.pause();
    this.clearStage(true);
    // anything attached in the chat goes with the first thing you say
    this.staged = staged.splice(0);
    renderStaged();
    this.renderTray();
    $("#live-mute").setAttribute("aria-pressed", "false");
    $("#live-mute-label").textContent = "Mute";
    $("#live-log").replaceChildren(el("li", "empty", "Say something when you're ready. What you say and what it answers will show here."));
    $("#live").hidden = false;
    document.body.classList.add("live-on");
    showSide(prefs.get("liveSide", "") || null); // moves the panel into the overlay
    perf.reset();
    showPerf(prefs.get("livePerf", !matchMedia("(max-width: 860px)").matches));
    for (const n of $$("body > :not(#live)")) n.inert = true; // keep Tab inside the overlay
    syncWeb();
    this.orb ??= new SpeakingOrb($("#live-orb"));
    this.orb.levelSource = () => this.level;
    this.orb.pitchSource = () => this.pitch;
    this.orb.analyser = player.analyser;
    this.wearOrb();
    this.orb.state = "listening";
    this.orb.endLine();
    this.orb.start();
    this.setPhase("starting", "Starting the microphone");
    cancelAnimationFrame(this.glowFrame);
    this.glow();
    $("#live-end").focus();
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
      this.ctx = new AudioContext();
      const url = URL.createObjectURL(new Blob([TAP], { type: "text/javascript" }));
      await this.ctx.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);
      if (!this.open) return this.release();
      const source = this.ctx.createMediaStreamSource(this.stream);
      this.tap = new AudioWorkletNode(this.ctx, "tap");
      const sink = this.ctx.createGain();
      sink.gain.value = 0; // the worklet only runs when connected through to the output
      source.connect(this.tap).connect(sink).connect(this.ctx.destination);
      this.mic = this.ctx.createAnalyser(); // for the orbs that trace your voice
      this.mic.fftSize = 1024;
      source.connect(this.mic);
      this.rate = this.ctx.sampleRate;
      this.pre = [];
      this.speech = null;
      this.loud = 0;
      this.tap.port.onmessage = (e) => this.frame(e.data);
      this.listen();
    } catch (err) {
      this.release();
      this.setPhase("error", micErrorText(err));
    }
  },

  // the orb picked in the top bar: the lab's own, or one from VoiceOrbs
  wearOrb() {
    const id = prefs.get("liveOrb", LAB);
    $("#live-orb-pick").value = id;
    if (this.orb) wear(this.orb, id, () => (this.phase === "speaking" || !this.mic ? player.analyser : this.mic));
  },

  release() {
    try { this.tap?.port && (this.tap.port.onmessage = null); } catch {}
    this.stream?.getTracks().forEach((t) => t.stop());
    this.ctx?.close().catch(() => {});
    this.stream = this.ctx = this.tap = this.mic = null;
  },

  async end() {
    if (!this.open) return;
    this.open = false;
    clearTimeout(this.timer);
    this.release();
    player.stop();
    if (current) await stopReply();
    this.orb?.pause();
    this.orb?.endLine();
    cancelAnimationFrame(this.glowFrame);
    $("#live").hidden = true;
    document.body.classList.remove("live-on");
    staged.push(...this.staged); // not sent yet: back to the message box
    this.staged = [];
    renderStaged();
    showSide(!side.hidden && sideTab === "log" ? "log" : null); // the log can stay open beside the chat
    for (const n of $$("body > :not(#live)")) n.inert = false;
    this.phase = "idle";
    showWelcome();
    setActivity(document.body.dataset.activity || "");
    refreshControls();
    $("#live-button").focus();
  },

  modelGone() {
    if (this.phase === "error") return;
    player.stop();
    this.setPhase("error", "The model stopped. End this conversation and start the model again.");
  },

  // The window's edge glows with your voice while it listens and with its own while it speaks.
  glow() {
    if (!this.open) return;
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const level = still ? 0 : this.level, env = still ? 0 : this.orb?.env || 0;
    const g = {
      listening: 0.3 + level * 1.4, hearing: 0.45 + level * 1.4, sending: 0.6, thinking: 0.6,
      searching: 0.75, reading: 0.75, speaking: 0.45 + env * 0.55, done: 0.6, muted: 0.12, error: 0.7,
    }[this.phase] ?? 0.3;
    $("#live").style.setProperty("--glow", clamp(g, 0, 1).toFixed(3));
    this.glowFrame = requestAnimationFrame(() => this.glow());
  },

  // A new turn starts with a clean stage: no old words, step or page. Something you attached
  // stays up while you talk about it, until another card takes its place.
  clearStage(all = false) {
    this.lastYou = null;
    const you = $("#live-you");
    you.textContent = "";
    you.classList.remove("pending");
    $("#live-step").className = "live-step off";
    const card = $("#live-card");
    if (all || !card.classList.contains("shown")) card.hidden = true;
  },
  showStep(kind, label, line, web, tool) {
    const step = $("#live-step");
    step.className = `live-step ${kind}${web ? " web" : ""}`;
    workingOrb($(".tool-icon", step), kind === "pending" ? tool : null, web);
    $("b", step).textContent = label;
    $(".live-step-line", step).textContent = line;
  },
  showCard(src, name, result) {
    const card = $("#live-card"), img = $("img", card);
    const [title, source] = shotCaption(name, result);
    card.classList.remove("shown");
    card.title = "What the browser showed. Opens full size.";
    card.href = src;
    img.src = src;
    img.alt = `Screenshot: ${title}`;
    img.hidden = false;
    $("video", card).hidden = true;
    $("video", card).removeAttribute("src");
    $("b", card).textContent = title;
    $("small", card).textContent = source;
    card.hidden = false;
  },
  // the photo or video you sent, beside the orb while you talk about it
  showShown(item) {
    const card = $("#live-card"), img = $("img", card), video = $("video", card);
    if (!["image", "video"].includes(item.kind)) return;
    card.classList.add("shown");
    card.title = "What you attached. Opens full size.";
    card.href = item.url;
    img.hidden = item.kind !== "image";
    video.hidden = item.kind !== "video";
    if (item.kind === "image") { img.src = item.url; img.alt = item.name; video.removeAttribute("src"); }
    else { video.src = item.url; video.play().catch(() => {}); }
    $("b", card).textContent = item.name;
    $("small", card).textContent = item.kind === "image" ? "Your photo" : "Your video";
    card.hidden = false;
  },

  staged: [], // attachments that go with the next thing you say
  attach(files) {
    for (const file of files) {
      if (!guessKind(file)) { this.addLog("failed", "Didn't work", `${file.name} isn't an image, audio recording or video.`); continue; }
      this.staged.push(makeItem(file));
    }
    this.renderTray();
  },
  renderTray() {
    if (!this.open) return;
    const tray = $("#live-tray");
    tray.hidden = !this.staged.length;
    tray.replaceChildren(...this.staged.map((item) => chipFor(item, () => {
      this.staged = this.staged.filter((s) => s !== item);
      this.renderTray();
      $("#live-attach").focus();
    }, item.error || item.info ? itemDetail(item) + (item.error ? "" : ". Goes with what you say next.") : "Preparing…")));
    if (this.phase === "listening") $("#live-state").textContent = this.listeningText();
  },
  listeningText() {
    const ready = this.staged.filter((s) => !s.error).length;
    return ready ? `Listening. Ask about ${ready === 1 ? "it" : "them"}.` : PHASE_TEXT.listening;
  },

  setPhase(phase, text) {
    if (phase === "hearing") this.clearStage();
    this.phase = phase;
    $("#live").dataset.phase = phase;
    $("#live-state").textContent = text || (phase === "listening" ? this.listeningText() : PHASE_TEXT[phase]) || "";
    if (this.orb) this.orb.state = ORB_STATE[phase] || "listening";
    this.refresh();
  },
  refresh() {
    if (!this.open) return;
    $("#live-interrupt").disabled = !["sending", "thinking", "searching", "reading", "speaking"].includes(this.phase);
  },

  listen() {
    if (!this.open) return;
    this.speech = null;
    this.loud = 0;
    this.setPhase(this.muted ? "muted" : "listening");
  },

  frame(samples) {
    let sum = 0;
    for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    const rms = Math.sqrt(sum / samples.length);
    this.level += (Math.min(1, rms * 10) - this.level) * 0.35;
    if (this.muted) { this.level = 0; return; }
    if (this.phase === "hearing") {
      const f = pitchOf(samples, this.rate);
      if (f != null) this.pitch += (f - this.pitch) * 0.3;
    }
    this.pre.push(samples);
    if (this.pre.length > VAD.preFrames) this.pre.shift();

    const start = Math.max(0.012, this.floor * 3);
    const endLevel = Math.max(0.008, this.floor * 2);
    if (this.phase === "listening") {
      if (rms > start) this.loud++;
      else {
        this.loud = 0;
        this.floor = clamp(this.floor * 0.97 + rms * 0.03, 0.001, 0.05);
      }
      if (this.loud >= VAD.startFrames) {
        this.speech = [...this.pre];
        this.voiced = this.loud;
        this.quiet = 0;
        this.orb.endLine();
        this.setPhase("hearing");
      }
    } else if (this.phase === "hearing") {
      this.speech.push(samples);
      if (rms < endLevel) this.quiet++;
      else { this.quiet = 0; this.voiced++; }
      if (this.quiet >= VAD.endFrames || this.speech.length * FRAME >= VAD.maxSeconds) this.utterance();
    } else if ($("#live-barge").checked && ["thinking", "searching", "reading", "speaking"].includes(this.phase)) {
      // Cutting in needs clearly louder speech than starting a turn, so its own voice
      // leaking from speakers doesn't count. Headphones make this reliable.
      if (rms > Math.max(0.04, this.floor * 6)) this.loud++;
      else this.loud = 0;
      if (this.loud >= VAD.bargeFrames && !this.cutting) {
        this.cutting = true;
        this.interrupt().then(() => {
          this.cutting = false;
          if (!this.open) return;
          this.speech = [...this.pre];
          this.voiced = this.loud;
          this.quiet = 0;
          this.setPhase("hearing");
        });
      }
    }
  },

  async utterance() {
    perf.begin(performance.now() - this.quiet * FRAME * 1000); // when you stopped talking
    const chunks = this.speech.slice(0, this.speech.length - Math.max(0, this.quiet - 10));
    const voiced = this.voiced * FRAME;
    this.speech = null;
    if (voiced < VAD.minVoiced) { this.listen(); return; }
    this.setPhase("sending");
    const seconds = chunks.length * FRAME;
    // What you attached goes in the same message as your voice, just before it, so the
    // model takes "this" to mean the new photo rather than one from earlier.
    const shown = this.staged.filter((s) => !s.error);
    this.staged = this.staged.filter((s) => s.error);
    this.renderTray();
    const you = this.addLog("you", "You", `${shownWords(shown)}${fmtClock(seconds)}`, true);
    if (shown.length) {
      const row = el("div", "live-thumbs");
      for (const s of shown) { const t = el("span", "thumb"); t.append(thumb(s.kind, s.url)); row.append(t); }
      $("p", you).before(row);
      const pic = shown.find((s) => s.kind === "image" || s.kind === "video");
      if (pic) this.showShown(pic);
    }
    this.lastYou = you;
    $("#live-you").textContent = "…";
    $("#live-you").classList.add("pending");
    const giveBack = () => { this.staged.unshift(...shown); this.renderTray(); };
    if (!(await waitIdle())) { giveBack(); this.fail("The last reply is still going. Try again in a moment."); return; }
    const item = makeItem(wavFile(chunks, this.rate), true);
    item.promise.then(() => { if (item.info) this.yours.set(item.info.id, you); });
    this.reply = null;
    this.text = "";
    const sent = await send({ text: "", items: [...shown, item], reply: "voice", clear: false, hooks: this.hooks });
    if (!sent) giveBack();
    if (!sent && this.phase === "sending") this.fail("That didn't send. Say it again.");
  },

  async interrupt() {
    player.stop();
    this.orb?.endLine();
    if (current) await stopReply();
    await waitIdle(8000);
  },

  fail(message) {
    this.addLog("failed", "Didn't work", message);
    this.setPhase("error", message);
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.listen(), 2500);
  },

  yours: new Map(), // media id -> the "You" entry, to fill in what was said

  addLog(kind, who, text, pending = false) {
    const log = $("#live-log");
    $(".empty", log)?.remove();
    const li = el("li", kind);
    if (who) li.append(el("b", null, who));
    const p = el("p", pending ? "pending" : null, text);
    p.dir = "auto";
    li.append(p);
    log.append(li);
    log.scrollTop = log.scrollHeight;
    return li;
  },

  // events from the reply, via runTurn
  hooks: {
    started() { live.setPhase("thinking"); },
    token(text) {
      live.text += text;
      if (!live.reply) live.reply = live.addLog("it", "LFM3", "");
      $("p", live.reply).textContent = live.text.trim();
      if (live.orb.line) live.orb.setLineText(live.text);
      $("#live-log").scrollTop = $("#live-log").scrollHeight;
    },
    toolCall({ name, arguments: args }) {
      perf.toolCall(name);
      const web = WEB_TOOLS.has(name);
      live.setPhase(web ? (name === "web_search" ? "searching" : "reading") : "thinking");
      live.showStep("pending", TOOL_PENDING[name] || name, toolDetail(name, args, null), web, name);
      live.step = live.addLog(`step${web ? "" : " local"}`, null, `${TOOL_PENDING[name] || name}: ${toolDetail(name, args, null)}`);
      live.text = "";
      live.reply = null;
    },
    toolResult({ name, result, screenshot }) {
      perf.toolResult(name);
      if (live.step) $("p", live.step).textContent = `${TOOL_LABELS[name] || name}: ${toolDetail(name, null, result)}`;
      live.showStep(result?.error ? "failed" : "done", TOOL_LABELS[name] || name, toolDetail(name, null, result), WEB_TOOLS.has(name));
      if (screenshot) live.showCard(screenshot, name, result);
      live.setPhase("thinking");
      if (name === "add_reminder" && !result?.error) live.saved = true;
    },
    speaking(clock) {
      perf.firstSound();
      live.setPhase("speaking");
      live.orb.beginLine(clock, live.pace);
      live.orb.setLineText(live.text);
    },
    done(event, spoken) {
      perf.done(event.stats);
      const said = (event.text || "").trim();
      if (said && live.reply) $("p", live.reply).textContent = said;
      else if (said) live.reply = live.addLog("it", "LFM3", said);
      const secs = event.stats?.audio_seconds;
      if (live.orb.line && said) {
        live.orb.setLineText(said);
        if (secs) {
          live.orb.fitLine(secs);
          // learn how fast this voice talks, so the next reply's captions start closer
          const guess = estimateSeconds(said);
          if (guess > 1) live.pace = clamp(live.pace * 0.4 + (secs / guess) * 0.6, 0.6, 1.8);
        }
      }
      if (!spoken && live.open && !["error", "hearing"].includes(live.phase)) live.listen();
    },
    played(natural) {
      if (!live.open || live.phase !== "speaking") return;
      const next = () => {
        clearTimeout(live.timer);
        live.timer = setTimeout(() => {
          if (live.open && live.phase === "speaking") live.listen();
          if (live.open && live.phase === "done") live.listen();
        }, natural ? 250 : 0);
      };
      if (natural && live.saved) {
        live.saved = false;
        live.setPhase("done", "Saved");
        clearTimeout(live.timer);
        live.timer = setTimeout(() => live.open && live.phase === "done" && live.listen(), 1100);
        return;
      }
      next();
    },
    transcript({ media, text }) {
      const li = live.yours.get(media);
      if (!li) return;
      const p = $("p", li);
      p.textContent = text;
      p.classList.remove("pending");
      if (li === live.lastYou) {
        $("#live-you").textContent = text;
        $("#live-you").classList.remove("pending");
      }
    },
    trimmed({ left_out }) {
      perf.leftOut = left_out;
      live.addLog("step local", null, `Left out the ${left_out} earliest messages to fit the model's 32K context`);
    },
    fail(message) {
      perf.failed();
      if (live.open && live.phase !== "hearing") live.fail(message);
    },
  },
};

$$(".live-open").forEach((b) => b.addEventListener("click", () => live.start()));
$("#live-end").addEventListener("click", () => live.end());
$("#live-orb-pick").append(...ORB_CHOICES.map(([id, name]) => { const o = el("option", null, name); o.value = id; return o; }));
$("#live-orb-pick").addEventListener("change", (e) => {
  prefs.set("liveOrb", e.target.value);
  live.wearOrb();
});
$("#live-interrupt").addEventListener("click", async () => {
  await live.interrupt();
  live.listen();
});
$("#live-mute").addEventListener("click", () => {
  live.muted = !live.muted;
  const b = $("#live-mute");
  b.setAttribute("aria-pressed", String(live.muted));
  $("#live-mute-label").textContent = live.muted ? "Unmute" : "Mute";
  live.stream?.getAudioTracks().forEach((t) => (t.enabled = !live.muted));
  if (live.muted && live.phase === "hearing") { live.speech = null; live.listen(); }
  else if (["listening", "muted"].includes(live.phase)) live.listen();
});

$("#live-attach").addEventListener("click", () => $("#live-file").click());
$("#live-file").addEventListener("change", (e) => {
  live.attach([...e.target.files]);
  e.target.value = "";
});

$("#live-talk").addEventListener("click", () => toggleSide("talk"));
$("#live-logs").addEventListener("click", () => toggleSide("log"));
$("#live-stats").addEventListener("click", () => showPerf($("#live-perf").hidden));

// ---------- starters on the empty screen ----------
const STARTERS = {
  photo: { sample: "/samples/sign.png", text: "What does this sign say?" },
  transcribe: { sample: "/samples/speech.wav", text: "Transcribe this recording word for word.", reply: "text" },
  video: { sample: "/samples/clip.mp4", text: "Describe what happens in this clip and what you can hear." },
  reminder: { sample: "/samples/speech.wav", voice: true },
  time: { text: "What time is it in Tokyo right now?" },
  web: { text: "Look up the latest Python release.", reply: "text", tools: true, web: true },
  aloud: { text: "Read this aloud exactly: Everything you hear is generated on this Mac, with no internet connection.", reply: "voice" },
};
for (const button of $$("[data-starter]")) {
  button.addEventListener("click", async () => {
    const s = STARTERS[button.dataset.starter];
    player.prime();
    const items = [];
    if (s.sample) {
      const blob = await (await fetch(s.sample)).blob();
      const name = s.voice ? "voice-message.wav" : s.sample.split("/").pop();
      items.push(makeItem(new File([blob], name, { type: blob.type }), Boolean(s.voice)));
    }
    send({ text: s.text || "", items, reply: s.reply, tools: s.tools, web: s.web });
  });
}

// The voice memo's real waveform, drawn from its samples.
async function drawSampleWave() {
  const canvas = $("#sample-wave");
  if (!canvas) return;
  let samples;
  try {
    const data = await (await fetch("/samples/speech.wav")).arrayBuffer();
    samples = (await new OfflineAudioContext(1, 1, 16000).decodeAudioData(data)).getChannelData(0);
  } catch { return; }
  const paint = () => {
    const k = Math.min(devicePixelRatio || 1, 2), w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w) return;
    canvas.width = Math.round(w * k);
    canvas.height = Math.round(h * k);
    const g = canvas.getContext("2d");
    g.scale(k, k);
    g.fillStyle = "#dfe5f2";
    const bar = 2, gap = 1.5, n = Math.floor((w - 16) / (bar + gap)), per = Math.floor(samples.length / n);
    let peak = 0;
    for (let i = 0; i < samples.length; i++) peak = Math.max(peak, Math.abs(samples[i]));
    for (let i = 0; i < n; i++) {
      let m = 0;
      for (let j = i * per; j < (i + 1) * per; j++) m = Math.max(m, Math.abs(samples[j]));
      const a = Math.max(1, (m / (peak || 1)) * (h - 14));
      g.fillRect(8 + i * (bar + gap), (h - a) / 2, bar, a);
    }
  };
  paint();
  new ResizeObserver(paint).observe(canvas);
}

// ---------- keyboard ----------
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (live.open) live.end();
  else if (!$("#settings").hidden) openPanel($("#settings"), $("#open-settings"), false);
  else if (!side.hidden) showSide(null);
  else if (current || player.active) stopReply();
});

// ---------- phones ----------
const narrow = window.matchMedia("(max-width: 560px)");
const fitPlaceholder = () => {
  input.placeholder = narrow.matches ? "Message LFM3-Omni" : "Message LFM3-Omni, or attach an image, a recording or a video";
};
narrow.addEventListener("change", fitPlaceholder);
fitPlaceholder();

// ---------- go ----------
window.lfm3 = { live, player, barOrb, perf, mediaKind: (id) => mediaInfo.get(id)?.kind }; // for the UI test
syncWeb();
loadOptions();
updateFootNote();
renderStaged();
showWelcome();
setActivity("");
if (prefs.get("side", "") === "log") showSide("log");
barOrb.start(); // draw it once; it settles and pauses
drawSampleWave();
pollStatus();
setInterval(pollStatus, 1000);
input.focus();
