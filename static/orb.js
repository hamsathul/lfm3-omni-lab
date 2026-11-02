// The live-mode orb: a sphere of particles that changes shape with the conversation.
//
// Ported to plain JavaScript from "Speaking Orb" by Ship Notes
// (github.com/aqualang89/shipnotes-components, components/speaking-orb), MIT licence,
// itself based on Signal Orb. The particle shapes, the syllable rings and the caption
// words that build out of particles follow the original.
//
// Changes for this app:
// - Light mode. Particles are ink on paper: normal "over" blending instead of additive
//   light, deeper state colours, and highlights in a darker ink instead of white.
// - Colours come from the app's palette: graphite listening, blue thinking, amber
//   searching the web, green speaking.
// - Speech comes from streamed Web Audio, not an <audio> element or speechSynthesis:
//   the page sets an analyser for the envelope and a clock for the captions, and feeds
//   the reply's text as it arrives.

const names = ["listening", "thinking", "searching", "speaking", "done"];
const colors = [
  [44, 56, 68],    // listening: graphite
  [47, 73, 209],   // thinking: the app's action blue
  [166, 98, 0],    // searching: the status rail's loading amber
  [22, 116, 82],   // speaking: deep green
  [30, 127, 90],   // done
];
const INK = [23, 33, 43];
const SPK = 3;
const tau = Math.PI * 2;
const cache = new Map();
const seedsFor = (count) => {
  // a dense Fibonacci sphere shows moire stripes, so past 1000 points each one gets a small fixed nudge
  const jitter = count > 1000 ? ((1 - 1000 / count) * 1.6) / Math.sqrt(count) : 0;
  const rnd = (n) => { const x = Math.sin(n * 12.9898) * 43758.5453; return x - Math.floor(x); };
  if (!cache.has(count))
    cache.set(count, Array.from({ length: count }, (_, i) => {
      const y0 = 1 - (2 * (i + 0.5)) / count, a = i * 2.399963229728653 + (rnd(i) - 0.5) * jitter * 6;
      const y = Math.max(-1, Math.min(1, y0 + (rnd(i + 0.37) - 0.5) * jitter));
      return { x: Math.cos(a) * Math.sqrt(1 - y * y), y, z: Math.sin(a) * Math.sqrt(1 - y * y), a, u: i / count, sa: Math.sin(a), ca: Math.cos(a) };
    }));
  return cache.get(count);
};
const bandTilt = [[0.32, 0], [-0.72, 1.02], [1.08, -0.82]].map(([tilt, rot]) => [Math.cos(tilt + 1), Math.sin(tilt + 1), Math.cos(rot), Math.sin(rot)]);
const autoCount = () => (matchMedia("(pointer: coarse)").matches ? 5000 : 9000);
const clamp = (v, a = 0, b = 1) => Math.max(a, Math.min(b, v));
const ease = (p) => p * p * (3 - 2 * p);

// rough syllables, to time words when only the text and the total length of speech are known
const syl = (w) => Math.max(1, (w.toLowerCase().replace(/[^a-z]/g, "").replace(/e$/, "").match(/[aeiouy]+/g) || []).length);
const pulseEnv = (words, t) => {
  let e = 0;
  for (const w of words) {
    if (t < w.start - 0.05 || t > w.end + 0.15) continue;
    const n = syl(w.w), len = Math.max(0.08, w.end - w.start), k = clamp((t - w.start) / len) * n;
    const within = k - Math.floor(k);
    e = Math.max(e, (0.55 + 0.45 * Math.sin(Math.PI * clamp(within * 1.15))) * clamp((t - w.start + 0.05) / 0.05) * clamp((w.end + 0.15 - t) / 0.15));
  }
  return e;
};
// captions: phrases split at sentence ends and every ~7 words, like real subtitles
const groupsOf = (words) => {
  const out = [];
  let cur = [];
  words.forEach((w, i) => {
    cur.push(i);
    if (/[.!?]$/.test(w.w) || (cur.length >= 4 && /[,;:]$/.test(w.w)) || cur.length >= 8) { out.push(cur); cur = []; }
  });
  if (cur.length) out.push(cur);
  return out;
};
// The model speaks at roughly 160 words a minute; this is only the first guess, and the
// timings are stretched to the real length of the speech as it arrives.
const wordLength = (w) => 0.15 + 0.105 * syl(w) + (/[,.!?;:]$/.test(w) ? 0.22 : 0);
// the first guess at how long a text takes to say, to learn the voice's pace from each reply
export const estimateSeconds = (text) => text.trim().split(/\s+/).filter(Boolean).reduce((s, w) => s + wordLength(w), 0);

const VS = `attribute vec2 p;attribute vec3 d;uniform vec2 res;varying float a;varying float s;
  void main(){float size=max(d.x,2.0);float k=min(1.0,d.x*d.x/(size*size));a=d.y*k;s=d.z*k;
  gl_Position=vec4(p/res*2.0-1.0,0.0,1.0)*vec4(1,-1,1,1);gl_PointSize=size;}`;
// premultiplied colour, composited "over" the paper: overlapping dots darken like ink
const FS = `precision mediump float;uniform vec3 col;uniform vec3 spk;varying float a;varying float s;
  void main(){float r=length(gl_PointCoord-.5)*2.0;if(r>1.0)discard;
  float dot=1.0-smoothstep(.3,.38,r);
  float sp=mix(1.0,.05,smoothstep(.08,.12,r))*(1.0-smoothstep(.17,.2,r));
  float al=min(1.0,dot*a+sp*s);
  gl_FragColor=vec4(col*dot*a+spk*sp*s,al);}`;
const setupGL = (canvas) => {
  const gl = canvas.getContext("webgl", { premultipliedAlpha: true, preserveDrawingBuffer: true, antialias: false });
  if (!gl) return null;
  const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
  const prog = gl.createProgram();
  gl.attachShader(prog, sh(gl.VERTEX_SHADER, VS));
  gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FS));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return null;
  gl.useProgram(prog);
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  const lp = gl.getAttribLocation(prog, "p"), ld = gl.getAttribLocation(prog, "d");
  gl.enableVertexAttribArray(lp);
  gl.enableVertexAttribArray(ld);
  gl.vertexAttribPointer(lp, 2, gl.FLOAT, false, 20, 0);
  gl.vertexAttribPointer(ld, 3, gl.FLOAT, false, 20, 8);
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
  return { gl, res: gl.getUniformLocation(prog, "res"), col: gl.getUniformLocation(prog, "col"), spk: gl.getUniformLocation(prog, "spk") };
};
const sprite = (rgb, core) => {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d"), grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  grad.addColorStop(0, `rgba(${rgb},1)`);
  grad.addColorStop(core, `rgba(${rgb},1)`);
  grad.addColorStop(core + 0.04, `rgba(${rgb},0)`);
  grad.addColorStop(1, `rgba(${rgb},0)`);
  g.fillStyle = grad;
  g.fillRect(0, 0, 64, 64);
  return c;
};

export class SpeakingOrb {
  // dots: particle size, for small orbs whose dots would otherwise be specks
  constructor(root, { particles, dots = 1 } = {}) {
    this.dotScale = dots;
    this.root = root;
    this._state = "listening";
    this._level = 0.3;
    this._particlesOverride = particles ?? null;
    this.weights = names.map((n) => (n === this._state ? 1 : 0));
    this.time = 0;
    this.last = 0;
    this.slow = 0;
    this.env = 0;
    this.onsets = [];
    this.line = null;
    this.frame = 0;
    this.running = false;
    this.glyphCache = new Map();
    this.levelSource = null; // () => 0..1, the microphone while listening
    this.analyser = null;    // AnalyserNode on the reply's audio while speaking
    root.classList.add("speaking-orb-root");
    root.innerHTML = `<div class="orb"><canvas aria-hidden="true"></canvas><canvas aria-hidden="true"></canvas></div>
      <div class="cap" aria-live="off"></div><canvas class="fx" aria-hidden="true"></canvas>`;
    const canvases = root.querySelectorAll("canvas");
    this.canvas = canvases[0];
    this.dots = canvases[1];
    this.fx = canvases[2];
    this.ctx = this.canvas.getContext("2d");
    this.capEl = root.querySelector(".cap");
    this.fctx = this.fx.getContext("2d");
    this.gl = setupGL(this.dots);
    if (!this.gl) this.dctx = this.dots.getContext("2d");
    this.media = matchMedia("(prefers-reduced-motion: reduce)");
    this.resize = () => {
      const w = this.canvas.parentElement.getBoundingClientRect().width,
        size = Math.max(1, Math.round(w * Math.min(devicePixelRatio || 1, 2)));
      this.canvas.width = this.canvas.height = this.dots.width = this.dots.height = size;
      this.root.style.setProperty("--cap-size", Math.round(clamp(w * 0.07, 19, 34)) + "px");
      const hr = this.root.getBoundingClientRect(), k = Math.min(devicePixelRatio || 1, 2);
      this.fx.width = Math.round(hr.width * k);
      this.fx.height = Math.round(hr.height * k);
      this.glyphCache = new Map();
      this.draw(this.time);
    };
    this.observer = new ResizeObserver(this.resize);
    this.observer.observe(this.root);
    this.resize();
    this.root.setAttribute("role", "img");
    this.root.setAttribute("aria-label", `Assistant ${this._state}`);
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.last = 0;
    this.frame = requestAnimationFrame(this.tick);
  }
  pause() {
    this.running = false;
    cancelAnimationFrame(this.frame);
  }
  destroy() {
    this.pause();
    this.observer.disconnect();
  }

  get state() { return this._state; }
  set state(v) {
    this._state = names.includes(v) ? v : "listening";
    this.root.setAttribute("aria-label", `Assistant ${this._state}`);
  }
  get particles() {
    if (this._particlesOverride != null) return this._particlesOverride;
    return (this.auto ??= this.gl ? autoCount() : 1000);
  }

  // ---- captions for a spoken reply ----------------------------------------------
  // clock(): seconds of the reply heard so far. Text arrives a little ahead of the speech.
  // pace: how much slower (above 1) or faster than the first guess this voice speaks.
  beginLine(clock, pace = 1) {
    this.line = { words: [], text: "", clock, spoken: null, pace };
  }
  setLineText(text) {
    const L = this.line;
    if (!L || text === L.text) return;
    L.text = text;
    const toks = text.trim().split(/\s+/).filter(Boolean);
    let t = 0;
    const words = toks.map((w, i) => {
      const old = L.words[i];
      const len = wordLength(w) * L.pace;
      const word = old && old.w === w ? old : { w, start: t, end: t + len - (/[,.!?;:]$/.test(w) ? 0.22 * L.pace : 0) };
      t = word.start + len;
      return word;
    });
    L.words = words;
    L.groups = null;
    if (L.spoken) this.fitLine(L.spoken);
  }
  // Stretch the word timings over the speech heard so far (or all of it, once it's known).
  fitLine(seconds) {
    const L = this.line;
    if (!L || !L.words.length || !seconds) return;
    L.spoken = seconds;
    const now = L.clock(), last = L.words.at(-1);
    const scale = seconds / Math.max(0.1, last.start + wordLength(last.w) * L.pace);
    for (const w of L.words) {
      if (w.start <= now) continue; // a word already on screen never moves back
      w.start = Math.max(now, w.start * scale);
      w.end = Math.max(w.start + 0.08, w.end * scale);
    }
  }
  endLine() { this.line = null; }

  tick = (now) => {
    if (!this.running) return;
    const dt = this.last ? Math.min((now - this.last) / 1000, 0.05) : 0;
    this.last = now;
    this.time += dt;
    if (this._particlesOverride == null && dt) {
      this.slow = dt > 0.025 ? this.slow + 1 : 0;
      if (this.slow > 30 && (this.auto ?? 0) > 800) {
        this.auto = Math.max(800, Math.round((this.auto ?? 1000) * 0.6));
        this.slow = 0;
      }
    }
    const k = 1 - Math.exp(-dt * 7);
    this.weights = this.weights.map((w, i) => w + ((names[i] === this._state ? 1 : 0) - w) * k);
    const L = this.line;
    let raw = 0, lt = 0;
    if (L) {
      lt = L.clock();
      if (this.analyser && this._state === "speaking") {
        this.fbuf ??= new Float32Array(this.analyser.fftSize);
        this.analyser.getFloatTimeDomainData(this.fbuf);
        let s = 0;
        for (const v of this.fbuf) s += v * v;
        raw = clamp(Math.sqrt(s / this.fbuf.length) * 5);
      } else raw = pulseEnv(L.words, lt);
    }
    const prev = this.env;
    this.env += (raw - this.env) * (raw > this.env ? 0.55 : 0.18);
    // a syllable is a fast rise in the envelope; each one sends a ring up the sphere
    if (this.env - prev > 0.06 && this.time - (this.onsets.at(-1) ?? -1) > 0.12) this.onsets.push(this.time);
    this.onsets = this.onsets.filter((o) => this.time - o < 1.2);
    if (this.levelSource) this._level = Number(this.levelSource().toFixed(3));
    const still = this.media.matches;
    this.paint(still ? 0 : this.time, this.weights, { env: still ? 0 : this.env, onsets: still ? [] : this.onsets });
    this.caption(L ? lt : 0, L);
    this.frame = requestAnimationFrame(this.tick);
  };
  draw(t) {
    this.paint(t, this.weights, { env: this.env || 0, onsets: this.onsets || [] });
  }

  caption(t, L) {
    const el = this.capEl;
    if (!L || !L.words.length) {
      if (el.childElementCount) el.textContent = "";
      el.dataset.g = "";
      this.flight(t, []); // clears words still in flight, e.g. after an interrupt
      return;
    }
    const W = L.words, G = (L.groups ??= groupsOf(W));
    let gi = -1;
    G.forEach((g, i) => { if (t >= W[g[0]].start - 0.04) gi = i; });
    const last = W.at(-1).end, fade = L.spoken ? clamp(1 - (t - last - 1.8) / 0.4) : 1;
    if (gi < 0 || fade <= 0) {
      if (el.childElementCount) el.textContent = "";
      el.dataset.g = "";
      this.flight(t, []); // clears words still in flight, e.g. after an interrupt
      return;
    }
    const key = gi + ":" + G[gi].length;
    if (el.dataset.g !== key) {
      el.dataset.g = key;
      el.textContent = "";
      for (const i of G[gi]) {
        const s = document.createElement("span");
        s.textContent = W[i].w + " ";
        el.append(s);
      }
    }
    const reduce = this.media.matches;
    [...el.children].forEach((s, k) => {
      const w = W[G[gi][k]];
      if (!w) return;
      // words are drawn in particles first; the real text shows as they land
      const a = reduce ? (t >= w.start ? 1 : 0) : ease(clamp((t - w.start - 0.3) / 0.14));
      const on = t >= w.start - 0.04 && t < w.end + 0.12;
      s.style.opacity = (a * fade * (on ? 1 : 0.55)).toFixed(3);
    });
    this.flight(t, reduce ? [] : G[gi].map((i, k) => [W[i], el.children[k]]));
  }

  // glyph points of one word, sampled from the caption font, relative to its span box
  glyphs(word, span) {
    const cs = getComputedStyle(this.capEl), font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`, r = span.getBoundingClientRect();
    const key = word + "|" + font;
    if (this.glyphCache.has(key)) return this.glyphCache.get(key);
    const w = Math.ceil(r.width) + 4, h = Math.ceil(r.height), c = document.createElement("canvas");
    c.width = Math.max(1, w);
    c.height = Math.max(1, h);
    const g = c.getContext("2d");
    g.font = font;
    g.fillStyle = "#000";
    const m = g.measureText(word), A = m.fontBoundingBoxAscent, D = m.fontBoundingBoxDescent;
    g.fillText(word, 0, (h - (A + D)) / 2 + A);
    const d = g.getImageData(0, 0, c.width, c.height).data, pts = [], fs = parseFloat(cs.fontSize), step = Math.max(1, fs / 22);
    for (let y = 0; y < h; y += step)
      for (let x = 0; x < w; x += step) {
        const i = ((y | 0) * c.width + (x | 0)) * 4 + 3;
        if (d[i] > 140) pts.push([x, y]);
      }
    this.glyphCache.set(key, pts);
    return pts;
  }
  flight(t, items) {
    const g = this.fctx, W = this.fx.width, H = this.fx.height;
    if (!W) return;
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, W, H);
    if (!items.length) return;
    const hr = this.root.getBoundingClientRect(), k = W / hr.width, orb = this.canvas.getBoundingClientRect();
    const cx = orb.left - hr.left + orb.width / 2, cy = orb.top - hr.top + orb.height / 2, R = orb.width * 0.25;
    const rnd = (n) => { const x = Math.sin(n * 12.9898) * 43758.5453; return x - Math.floor(x); };
    g.setTransform(k, 0, 0, k, 0, 0);
    const [cr, cg, cb] = colors[SPK], dot = Math.max(1, parseFloat(getComputedStyle(this.capEl).fontSize) / 24);
    items.forEach(([w, span], wi) => {
      const age = t - w.start + 0.06;
      if (!w || age < 0 || age > 0.72 || !span) return;
      const pts = this.glyphs(w.w, span), sr = span.getBoundingClientRect(),
        ox = sr.left - hr.left, oy = sr.top - hr.top, seed = w.start * 977 + wi;
      for (let j = 0; j < pts.length; j++) {
        const r1 = rnd(seed + j * 1.7), r2 = rnd(seed + j * 3.1 + 0.3), r3 = rnd(seed + j * 5.3 + 0.7),
          del = r1 * 0.12, p = clamp((age - del) / 0.36);
        if (p <= 0) continue;
        const land = clamp((age - del - 0.34) / 0.1), al = (1 - land) * Math.min(1, p * 5);
        if (al <= 0) continue;
        const e = 1 - Math.pow(1 - p, 3), a0 = Math.PI / 2 + (r2 - 0.5) * 1.5;
        const sx = cx + Math.cos(a0) * R * (0.85 + 0.15 * r3), sy = cy + Math.sin(a0) * R * (0.92 + 0.08 * r3);
        const tx = ox + pts[j][0], ty = oy + pts[j][1], mx = (sx + tx) / 2 + (r3 - 0.5) * R * 0.9, my = (sy + ty) / 2 + (r2 - 0.5) * R * 0.25;
        const x = (1 - e) * (1 - e) * sx + 2 * (1 - e) * e * mx + e * e * tx, y = (1 - e) * (1 - e) * sy + 2 * (1 - e) * e * my + e * e * ty;
        const wm = e * e; // the green of the voice settles into the ink of the caption
        g.fillStyle = `rgba(${(cr + (INK[0] - cr) * wm) | 0},${(cg + (INK[1] - cg) * wm) | 0},${(cb + (INK[2] - cb) * wm) | 0},${(al * 0.85).toFixed(3)})`;
        const s = dot * (1.4 - 0.5 * e);
        g.fillRect(x - s / 2, y - s / 2, s, s);
      }
    });
  }

  paint(t, w, { env, onsets }) {
    const ctx = this.ctx, size = this.canvas.width;
    if (!size) return;
    const unit = size / 720;
    ctx.setTransform(unit, 0, 0, unit, 0, 0);
    ctx.clearRect(0, 0, 720, 720);
    const rgb = [0, 1, 2].map((c) => Math.round(colors.reduce((s, col, i) => s + col[c] * w[i], 0))), color = rgb.join(",");
    const ws = w[SPK], glow = 0.05 + 0.045 * env * ws;
    const halo = ctx.createRadialGradient(360, 360, 10, 360, 360, 305 + 25 * env * ws);
    halo.addColorStop(0, `rgba(${color},${glow})`);
    halo.addColorStop(0.57, `rgba(${color},${glow * 0.45})`);
    halo.addColorStop(1, `rgba(${color},0)`);
    ctx.fillStyle = halo;
    ctx.fillRect(0, 0, 720, 720);
    const count = this.particles, seeds = seedsFor(count), ring = Math.round(count * 0.65);
    const fine = Math.max(0.5, Math.pow(1000 / count, 0.55)), light = Math.min(1, Math.pow(1000 / (count * fine * fine), 0.35)) * 1.05;
    if (!this.buf || this.buf.length < count * 5) this.buf = new Float32Array(count * 5);
    const buf = this.buf, [w0, w1, w2, w3, w4] = w, spin = t * 0.23, cs = Math.cos(spin), sn = Math.sin(spin), wave = 8 + this._level * 22;
    const rings = onsets.map((o) => { const a = t - o; return { h: -1.15 + a * 2.1, g: Math.pow(1 - a / 1.2, 2) }; });
    for (let i = 0; i < count; i++) {
      const p = seeds[i];
      let vx = 0, vy = 0, vz = 0, lit = 0;
      if (w0 > 0.001) { // listening: a sphere rippling with the microphone
        const x = p.x * cs + p.z * sn, z = p.z * cs - p.x * sn;
        const r = (192 + Math.sin(p.y * 11 - t * 3 + p.a * 0.03) * wave + Math.sin(p.a * 0.17 + t * 2) * 5) * w0;
        vx += x * r; vy += p.y * r; vz += z * r;
      }
      if (w1 > 0.001) { // thinking: a turning torus knot
        const theta = p.u * tau * 3 + t * 0.57, phi = p.u * tau * 8 - t * 0.48, rt = 137 + 41 * Math.cos(phi) + 24 * p.sa;
        const bx = rt * Math.cos(theta), bz = rt * Math.sin(theta), by = 65 * Math.sin(phi) + 17 * p.y;
        vx += bx * w1; vy += (by * 0.77 - bz * 0.52) * w1; vz += (by * 0.52 + bz * 0.77) * w1;
      }
      if (w2 > 0.001) { // searching: three orbiting bands
        const band = i % 3, angle = p.u * tau * 5 + t * (0.7 + band * 0.2), radius = 196 + 11 * p.sa;
        const sx = Math.cos(angle) * radius, sy = Math.sin(angle) * radius;
        const sy2 = sy * bandTilt[band][0], sz = sy * bandTilt[band][1], cr2 = bandTilt[band][2], sr2 = bandTilt[band][3];
        vx += (sx * cr2 - sy2 * sr2) * w2; vy += (sx * sr2 + sy2 * cr2) * w2; vz += sz * w2;
      }
      if (w3 > 0.001) { // speaking: the voice inflates a lobed sphere; each syllable sends a ring up it
        const x = p.x * cs + p.z * sn, z = p.z * cs - p.x * sn;
        const lobe = Math.sin(x * 2.3 + t * 1.4) * Math.sin(p.y * 2.9 - t * 1.1) * Math.cos(z * 2.1 + t * 0.8);
        let r = 184 + 16 * env + lobe * (5 + 19 * env) + Math.sin(p.y * 7 - t * 2.2) * 3 * env;
        for (const R of rings) { const d = (p.y - R.h) / 0.15, g = Math.exp(-d * d) * R.g; r += 7 * g; lit += g; }
        vx += x * r * w3; vy += p.y * r * w3; vz += z * r * w3;
        lit *= w3;
      }
      if (w4 > 0.001) { // done: a ring and a tick
        if (i < ring) {
          const q = (i / ring) * tau, rr = 173 + 9 * p.sa;
          vx += Math.cos(q) * rr * w4; vy += Math.sin(q) * rr * w4; vz += 8 * p.sa * w4;
        } else {
          const q = (i - ring) / (count - ring), first = q < 0.38, v = first ? q / 0.38 : (q - 0.38) / 0.62;
          vx += ((first ? -81 + 58 * v : -23 + 116 * v) + 6 * p.sa) * w4;
          vy += ((first ? 57 * v : 57 - 133 * v) + 6 * p.ca) * w4;
        }
      }
      const depth = (vz + 240) / 480, persp = 850 / (850 - vz), alpha = 0.12 + 0.78 * depth + 0.3 * env * ws;
      const r = (0.8 + depth * 1.35) * (0.75 + 0.25 * Math.sin(p.a + t * 1.6)) * fine * (1 + 0.5 * Math.min(1, lit)), o = i * 5;
      buf[o] = (360 + vx * persp) * unit;
      buf[o + 1] = (360 + vy * persp) * unit;
      buf[o + 2] = r * 9 * unit * this.dotScale;
      buf[o + 3] = Math.min(1, (alpha + 1.2 * lit) * light);
      buf[o + 4] = vz > 75 || lit > 0.25 ? (Math.max(alpha - 0.5, 0) + lit * 0.8) * 0.75 * light : 0;
    }
    if (this.gl) this.drawGL(buf, count, rgb, size);
    else this.draw2D(buf, count, color, size);
  }
  drawGL(buf, count, rgb, size) {
    const { gl, res, col, spk } = this.gl;
    gl.viewport(0, 0, size, size);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.uniform2f(res, size, size);
    gl.uniform3f(col, rgb[0] / 255, rgb[1] / 255, rgb[2] / 255);
    // highlights in a deeper ink of the same colour: on paper, white would vanish
    gl.uniform3f(spk, (rgb[0] * 0.45) / 255, (rgb[1] * 0.45) / 255, (rgb[2] * 0.45) / 255);
    gl.bufferData(gl.ARRAY_BUFFER, buf.subarray(0, count * 5), gl.DYNAMIC_DRAW);
    gl.drawArrays(gl.POINTS, 0, count);
  }
  draw2D(buf, count, color, size) {
    const ctx = this.dctx;
    if (this.spriteColor !== color) {
      this.spriteColor = color;
      this.dot = sprite(color, 0.34);
      this.spark = sprite(color.split(",").map((c) => Math.round(c * 0.45)).join(","), 0.5);
    }
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, size, size);
    for (let i = 0; i < count; i++) {
      const o = i * 5, x = buf[o], y = buf[o + 1], s = buf[o + 2];
      ctx.globalAlpha = buf[o + 3];
      ctx.drawImage(this.dot, x - s / 2, y - s / 2, s, s);
      if (buf[o + 4] > 0) {
        const h = s * 0.2;
        ctx.globalAlpha = buf[o + 4];
        ctx.drawImage(this.spark, x - h / 2, y - h / 2, h, h);
      }
    }
    ctx.globalAlpha = 1;
  }
}
