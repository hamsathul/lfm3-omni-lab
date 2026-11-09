//! The orb: a sphere of particles drawn as one vector shape, after the web page's lab orb
//! (static/orb.js). Each particle is a zero-length round-capped segment, so one `Shape` draws
//! them all with their own colour and depth shading. It breathes while it waits, leans in and
//! ripples while you talk, swirls while the model thinks and pulses with the voice as it speaks.

use rune::{Color, El, LineCap, Shape, canvas};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mood {
    /// Waiting for you to talk.
    Listening,
    /// You are talking.
    Hearing,
    /// The model is working on an answer.
    Thinking,
    /// A tool or web step is running.
    Searching,
    /// The answer is being spoken.
    Speaking,
    /// The microphone is off.
    Muted,
    Error,
}

impl Mood {
    /// The state colour, from the web orb's palette, lifted for a dark background.
    pub fn color(self, dark: bool) -> Color {
        let (light, night) = match self {
            Mood::Listening => ([44, 56, 68], [196, 206, 218]),
            Mood::Hearing => ([31, 43, 56], [235, 240, 246]),
            Mood::Thinking => ([47, 73, 209], [112, 140, 255]),
            Mood::Searching => ([166, 98, 0], [255, 178, 64]),
            Mood::Speaking => ([22, 116, 82], [70, 214, 150]),
            Mood::Muted => ([128, 136, 146], [120, 128, 138]),
            Mood::Error => ([190, 40, 30], [255, 96, 80]),
        };
        let [r, g, b] = if dark { night } else { light };
        crate::look::rgb(r, g, b)
    }
}

struct Seed {
    x: f32,
    y: f32,
    z: f32,
    r: f32,
}

fn seeds(count: usize) -> Vec<Seed> {
    let rnd = |n: f32| {
        let x = (n * 12.9898).sin() * 43758.547;
        x - x.floor()
    };
    (0..count)
        .map(|i| {
            let y = 1.0 - 2.0 * (i as f32 + 0.5) / count as f32;
            let a = i as f32 * 2.399_963_2;
            let ring = (1.0 - y * y).max(0.0).sqrt();
            Seed { x: a.cos() * ring, y, z: a.sin() * ring, r: rnd(i as f32 + 0.71) }
        })
        .collect()
}

thread_local! {
    static SEEDS: std::cell::RefCell<Vec<(usize, std::rc::Rc<Vec<Seed>>)>> = const { std::cell::RefCell::new(Vec::new()) };
}

fn seeds_for(count: usize) -> std::rc::Rc<Vec<Seed>> {
    SEEDS.with(|cache| {
        let mut cache = cache.borrow_mut();
        if let Some((_, s)) = cache.iter().find(|(n, _)| *n == count) {
            return s.clone();
        }
        let s = std::rc::Rc::new(seeds(count));
        cache.push((count, s.clone()));
        s
    })
}

/// The particles at time `t` (seconds), for a square of `size` points. `level` is 0..1: your
/// voice while hearing, the reply's voice while speaking.
pub fn shape(size: f32, t: f32, mood: Mood, level: f32, dark: bool) -> Shape {
    let count = if size >= 120.0 { 1100 } else if size >= 48.0 { 360 } else { 140 };
    let seeds = seeds_for(count);
    let base = mood.color(dark);
    let level = level.clamp(0.0, 1.0);
    let c = size * 0.5;
    let radius = size * 0.36;
    let dot = (size / 90.0).clamp(1.1, 2.6);
    // The whole sphere turns slowly; thinking and searching spin it faster.
    let spin = match mood {
        Mood::Thinking | Mood::Searching => 0.9,
        Mood::Muted => 0.08,
        _ => 0.22,
    } * t;
    let (sy, cy) = spin.sin_cos();
    let (sx, cx) = (0.35f32).sin_cos();
    let breathe = 1.0 + 0.025 * (t * 1.3).sin();
    let mut shape = Shape::new();
    for (i, p) in seeds.iter().enumerate() {
        // Per-mood displacement along the radius.
        let wobble = match mood {
            Mood::Listening => 0.012 * (t * 1.7 + p.r * 6.0).sin(),
            Mood::Hearing => level * 0.16 * (p.y * 7.0 - t * 9.0).sin() + 0.03 * level,
            Mood::Speaking => level * 0.12 * (p.y * 5.0 + t * 6.0).sin().abs() + 0.04 * level,
            Mood::Thinking => 0.05 * ((p.y * 9.0 + t * 3.0).sin() * (p.x * 7.0).cos()),
            Mood::Searching => 0.07 * ((p.y * 14.0 - t * 4.0).sin()),
            Mood::Muted => -0.18,
            Mood::Error => 0.05 * (t * 40.0 + i as f32).sin(),
        };
        let r = breathe * (1.0 + wobble);
        let (x, y, z) = (p.x * r, p.y * r, p.z * r);
        // Spin about y, then tilt toward the viewer about x.
        let (x, z) = (x * cy + z * sy, -x * sy + z * cy);
        let (y, z) = (y * cx - z * sx, y * sx + z * cx);
        let depth = (z + 1.0) * 0.5; // 0 far, 1 near
        let lean = if mood == Mood::Hearing { 1.0 + 0.08 * level * depth } else { 1.0 };
        let px = c + x * radius * lean;
        let py = c - y * radius * lean;
        let alpha = (0.12 + 0.88 * depth.powf(1.6)) * if mood == Mood::Muted { 0.55 } else { 1.0 };
        let w = dot * (0.55 + 0.65 * depth);
        // Near particles shade toward a highlight, far ones toward the base colour.
        let hi = if dark { 0.3 * depth } else { 0.0 };
        let col = Color {
            r: (base.r + hi * (1.0 - base.r)).clamp(0.0, 1.0),
            g: (base.g + hi * (1.0 - base.g)).clamp(0.0, 1.0),
            b: (base.b + hi * (1.0 - base.b)).clamp(0.0, 1.0),
            a: alpha,
        };
        shape = shape.line([px, py], [px + 0.01, py], w, LineCap::Round, col);
    }
    shape
}

/// An orb element `size` points square.
pub fn orb(size: f32, t: f32, mood: Mood, level: f32, dark: bool) -> El {
    canvas(shape(size, t, mood, level, dark)).size(size, size)
}
