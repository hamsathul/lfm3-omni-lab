// VoiceOrbs (MIT, (c) 2026 Alexis Munoz, github.com/amunozdev/voiceorbs), bundled with React
// so the lab page can mount any of them on a plain element.
import { createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { PlasmaOrb } from './src/registry/orbe/plasma-orb/plasma-orb';
import { EqualizerOrb } from './src/registry/orbe/equalizer-orb/equalizer-orb';
import { HaloOrb } from './src/registry/orbe/halo-orb/halo-orb';
import { ParticlesOrb } from './src/registry/orbe/particles-orb/particles-orb';
import { SiriSheet } from './src/registry/orbe/siri-sheet/siri-sheet';
import { SiriWaveLine } from './src/registry/orbe/siri-wave-line/siri-wave-line';
import { PulseOrb } from './src/registry/orbe/pulse-orb/pulse-orb';
import { GlassOrb } from './src/registry/orbe/glass-orb/glass-orb';
import { AuroraOrb } from './src/registry/orbe/aurora-orb/aurora-orb';
import { GooeyOrb } from './src/registry/orbe/gooey-orb/gooey-orb';
import { GalaxyOrb } from './src/registry/orbe/galaxy-orb/galaxy-orb';
import { NebulaOrb } from './src/registry/orbe/nebula-orb/nebula-orb';
import { WaveformRing } from './src/registry/orbe/waveform-ring/waveform-ring';
import { IridescentFlow } from './src/registry/orbe/iridescent-flow/iridescent-flow';
import { MercuryOrb } from './src/registry/orbe/mercury-orb/mercury-orb';
import { MinimalOrb } from './src/registry/orbe/minimal-orb/minimal-orb';
import { DitherOrb } from './src/registry/orbe/dither-orb/dither-orb';
import { GrainOrb } from './src/registry/orbe/grain-orb/grain-orb';
import { AuraField } from './src/registry/orbe/aura-field/aura-field';
import { EdgeGlow } from './src/registry/orbe/edge-glow/edge-glow';

export const ORBS = {
  'plasma-orb': { name: "Plasma Orb", tagline: "Shader mesh gradient on canvas, no Three.js. Organic distortion.", tech: "Shader (canvas)", colorFrom: '#7c3aed', colorTo: '#06b6d4', component: PlasmaOrb },
  'equalizer-orb': { name: "Equalizer Orb", tagline: "Equalizer bars inside a disc that react to the audio level.", tech: "Pure CSS", colorFrom: '#38bdf8', colorTo: '#818cf8', component: EqualizerOrb },
  'halo-orb': { name: "Halo Orb", tagline: "A conic halo with orbital rings and a bright core that pulses.", tech: "Pure CSS", colorFrom: '#818cf8', colorTo: '#f472b6', component: HaloOrb },
  'particles-orb': { name: "Particles Orb", tagline: "Hundreds of particles form a rotating 3D sphere that breathes with your voice and scatters into a ring while connecting.", tech: "Canvas", colorFrom: '#f0abfc', colorTo: '#818cf8', component: ParticlesOrb },
  'siri-sheet': { name: "Siri Sheet", tagline: "Glass sphere with a luminous sheet of light inside; the rim refracts and splits color. Raw WebGL, zero dependencies.", tech: "Shader (canvas)", colorFrom: '#82f4ff', colorTo: '#8e6cff', component: SiriSheet },
  'siri-wave-line': { name: "Siri Wave Line", tagline: "Overlapping fluorescent sine waves that flatten at rest and surge with the voice.", tech: "Canvas", colorFrom: '#22d3ee', colorTo: '#e879f9', component: SiriWaveLine },
  'pulse-orb': { name: "Pulse Orb", tagline: "Core with expanding rings and a loading arc. Minimal and SSR-safe.", tech: "Pure CSS", colorFrom: '#818cf8', colorTo: '#22d3ee', component: PulseOrb },
  'glass-orb': { name: "Glass Orb", tagline: "Iridescent glassmorphism with a spinning conic aura and specular highlight.", tech: "Pure CSS", colorFrom: '#a78bfa', colorTo: '#38bdf8', component: GlassOrb },
  'aurora-orb': { name: "Aurora Orb", tagline: "Northern-lights veils that swirl and blur across a night sky.", tech: "Pure CSS", colorFrom: '#22d3ee', colorTo: '#a855f7', component: AuroraOrb },
  'gooey-orb': { name: "Gooey Orb", tagline: "Liquid blob whose edges \u201cboil\u201d with SVG noise and displacement.", tech: "SVG filters", colorFrom: '#f472b6', colorTo: '#8b5cf6', component: GooeyOrb },
  'galaxy-orb': { name: "Galaxy Orb", tagline: "A glassy bubble holding a drifting starfield and nebula, with a specular glare and an iridescent, chromatic rim.", tech: "Canvas", colorFrom: '#c084fc', colorTo: '#38bdf8', component: GalaxyOrb },
  'nebula-orb': { name: "Nebula Orb", tagline: "3D sphere with simplex-noise displacement and fresnel. The \u201cvoice mode\u201d.", tech: "WebGL (R3F + GLSL)", colorFrom: '#8b5cf6', colorTo: '#22d3ee', component: NebulaOrb },
  'waveform-ring': { name: "Waveform Ring", tagline: "A ring whose radius traces the live waveform: time-domain audio drawn in polar coordinates.", tech: "Canvas", colorFrom: '#2dd4bf', colorTo: '#38bdf8', component: WaveformRing },
  'iridescent-flow': { name: "Iridescent Flow", tagline: "Single-pass fragment shader with flowing iridescent hues. Raw WebGL, zero dependencies.", tech: "Shader (canvas)", colorFrom: '#c084fc', colorTo: '#67e8f9', component: IridescentFlow },
  'mercury-orb': { name: "Mercury Orb", tagline: "Paper liquid-metal shader: chrome ripples flowing around a molten core.", tech: "Shader (canvas)", colorFrom: '#cbd5e1', colorTo: '#a5b4fc', component: MercuryOrb },
  'minimal-orb': { name: "Minimal Orb", tagline: "A single quiet disc: just breath and color. As minimal as it gets.", tech: "Pure CSS", colorFrom: '#818cf8', colorTo: '#a5b4fc', component: MinimalOrb },
  'dither-orb': { name: "Dither Orb", tagline: "Retro ordered-dithering shader: a glowing sphere rendered in dancing dots.", tech: "Shader (canvas)", colorFrom: '#a3e635', colorTo: '#22d3ee', component: DitherOrb },
  'grain-orb': { name: "Grain Orb", tagline: "Grainy gradient blob with film-noise texture that swells with the audio level.", tech: "Shader (canvas)", colorFrom: '#fb923c', colorTo: '#e879f9', component: GrainOrb },
  'aura-field': { name: "Aura Field", tagline: "Undulating ring of energy that springs open when it starts listening. Raw WebGL, zero dependencies.", tech: "Shader (canvas)", colorFrom: '#34d399', colorTo: '#60a5fa', component: AuraField },
  'edge-glow': { name: "Edge Glow", tagline: "Siri-style ambient frame: a masked conic-gradient glow that wraps your own content instead of sitting in the middle.", tech: "Pure CSS", colorFrom: '#f472b6', colorTo: '#60a5fa', component: EdgeGlow }
};

/** Mount an orb in `el`. Returns set(props) to change state, size or colours, the level
 *  object (put 0..1 in .current for a live level, -1 for the orb's own idea of the state),
 *  and destroy(). */
export function mountOrb(el, id, props = {}) {
  const meta = ORBS[id];
  if (!meta) throw new Error(`no orb called ${id}`);
  const root = createRoot(el);
  const levelRef = { current: -1 };
  let current = { state: 'idle', size: 168, speed: 1, colorFrom: meta.colorFrom, colorTo: meta.colorTo, ...props, levelRef };
  const draw = () => root.render(createElement(meta.component, current));
  draw();
  return {
    id, levelRef,
    set(next) { current = { ...current, ...next, levelRef }; draw(); },
    destroy() { root.unmount(); },
  };
}

/** The page's own analyser for the orbs that draw the waveform, so they don't open the microphone again. */
export function useAnalyser(get) { globalThis.__voiceOrbsAnalyser = get; }
