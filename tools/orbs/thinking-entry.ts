// Thinking Orbs (MIT, (c) 2026 Jakub Antalik, github.com/Jakubantalik/Libraries.dev):
// the canvas engine and its nine tuned states, without React.
import { paintFrame } from '../libraries/packages/thinking-orbs/src/engine/core';
import { MODE_FRAMES } from '../libraries/packages/thinking-orbs/src/engine/registry';
import { resolvePreset, STATE_TO_MODE } from '../libraries/packages/thinking-orbs/src/presets';
import { scaleRadii } from '../libraries/packages/thinking-orbs/src/engine/profiles';

export const STATES = Object.keys(STATE_TO_MODE);

/** Draw a Thinking Orb on `canvas`. `size` is the drawn size in CSS pixels; the 64 tuning is
 *  used from 40 px up and the 20 tuning below. Returns set({state, color, size, speed, dark, dotSize}) and stop(). */
export function thinkingOrb(canvas: HTMLCanvasElement, opts: any = {}) {
  let o = { state: 'working', size: 64, speed: 1, dark: false, dotSize: 1, color: undefined as any, ...opts };
  const ctx = canvas.getContext('2d')!;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let frameFn: any, popts: any, speed = 1, tint: any, raf = 0, dpr = 1;
  const setup = () => {
    dpr = Math.min(2, devicePixelRatio || 1);
    canvas.width = Math.round(o.size * dpr); canvas.height = Math.round(o.size * dpr);
    canvas.style.width = canvas.style.height = `${o.size}px`;
    const p = resolvePreset(o.state, o.size >= 40 ? 64 : 20);
    frameFn = MODE_FRAMES[p.mode]; popts = o.dotSize !== 1 ? scaleRadii(p.opts, o.dotSize) : p.opts; speed = p.speed * o.speed;
    tint = o.color ? { r: o.color[0], g: o.color[1], b: o.color[2] } : undefined;
    canvas.setAttribute('role', 'img');
  };
  const draw = (t: number) => {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, o.size, o.size);
    paintFrame(ctx, frameFn(o.size, t, popts), o.dark, tint);
  };
  // stops for good once the canvas has been on the page and is taken off it
  let placed = false;
  const loop = () => {
    if (canvas.isConnected) placed = true;
    else if (placed) return;
    draw(reduced.matches ? 0.6 : (performance.now() / 1000) * speed);
    raf = requestAnimationFrame(loop);
  };
  setup(); loop();
  return {
    set(next: any) { o = { ...o, ...next }; setup(); },
    stop() { cancelAnimationFrame(raf); },
  };
}
