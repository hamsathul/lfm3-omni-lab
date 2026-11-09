//! The live-mode voice detector, ported from the web page (static/app.js, `frame()` and
//! `utterance()`): 20 ms frames, an adaptive noise floor, 80 ms of loud frames to start a turn,
//! 0.8 s of quiet to end it, 300 ms kept from before the start, and talk-over while the model
//! is busy. Pure: it gets samples and says what happened, so it is tested without a microphone.

use std::collections::VecDeque;

pub const FRAME_SECONDS: f32 = 0.02;
const START_FRAMES: u32 = 4;
const END_FRAMES: u32 = 40;
const PRE_FRAMES: usize = 15;
const MIN_VOICED_SECONDS: f32 = 0.35;
const MAX_SECONDS: f32 = 30.0;
const BARGE_FRAMES: u32 = 8;
const KEEP_QUIET_FRAMES: u32 = 10;

/// What the app is doing, which decides what the detector listens for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    /// Waiting for you: loud frames start a turn.
    Listen,
    /// The model is thinking or speaking. With `barge`, loud speech cuts in.
    Busy { barge: bool },
    /// The microphone is muted: frames are dropped.
    Muted,
}

#[derive(Debug, PartialEq)]
pub enum Heard {
    /// You started talking.
    Began,
    /// You talked over the reply (talk-over is on): stop it; the turn is already being heard.
    Barge,
    /// A finished turn: mono samples at the detector's rate.
    Utterance(Vec<f32>),
    /// A turn too short to be speech (a cough, a click): dropped.
    TooShort,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Phase {
    Waiting,
    Hearing,
}

pub struct Vad {
    rate: u32,
    frame_len: usize,
    partial: Vec<f32>,
    phase: Phase,
    floor: f32,
    loud: u32,
    quiet: u32,
    voiced: u32,
    pre: VecDeque<Vec<f32>>,
    speech: Vec<Vec<f32>>,
    last_mode: Mode,
    /// The latest frame's RMS level, for meters.
    pub level: f32,
}

impl Vad {
    pub fn new(rate: u32) -> Vad {
        Vad {
            rate,
            frame_len: ((rate as f32) * FRAME_SECONDS).round() as usize,
            partial: Vec::new(),
            phase: Phase::Waiting,
            floor: 0.004,
            loud: 0,
            quiet: 0,
            voiced: 0,
            pre: VecDeque::new(),
            speech: Vec::new(),
            last_mode: Mode::Listen,
            level: 0.0,
        }
    }

    pub fn rate(&self) -> u32 {
        self.rate
    }

    /// Forget a turn in progress (live mode ended, or the turn was abandoned).
    pub fn reset(&mut self) {
        self.phase = Phase::Waiting;
        self.loud = 0;
        self.quiet = 0;
        self.voiced = 0;
        self.speech.clear();
        self.partial.clear();
    }

    /// Feed samples in any amount; returns what happened in the whole frames among them.
    pub fn feed(&mut self, samples: &[f32], mode: Mode) -> Vec<Heard> {
        let mut out = Vec::new();
        if mode != self.last_mode {
            // A change of mode starts the loud count afresh, as the page's phase changes do.
            self.loud = 0;
            self.last_mode = mode;
        }
        self.partial.extend_from_slice(samples);
        let mut start = 0;
        while self.partial.len() - start >= self.frame_len {
            let frame = self.partial[start..start + self.frame_len].to_vec();
            start += self.frame_len;
            if let Some(h) = self.frame(frame, mode) {
                out.push(h);
            }
        }
        self.partial.drain(..start);
        out
    }

    fn frame(&mut self, frame: Vec<f32>, mode: Mode) -> Option<Heard> {
        let rms = (frame.iter().map(|x| x * x).sum::<f32>() / frame.len().max(1) as f32).sqrt();
        self.level = rms;
        if mode == Mode::Muted {
            return None;
        }
        self.pre.push_back(frame.clone());
        while self.pre.len() > PRE_FRAMES {
            self.pre.pop_front();
        }
        let start_level = (self.floor * 3.0).max(0.012);
        let end_level = (self.floor * 2.0).max(0.008);
        match (self.phase, mode) {
            (Phase::Waiting, Mode::Listen) => {
                if rms > start_level {
                    self.loud += 1;
                } else {
                    self.loud = 0;
                    self.floor = (self.floor * 0.97 + rms * 0.03).clamp(0.001, 0.05);
                }
                if self.loud >= START_FRAMES {
                    self.begin_hearing();
                    return Some(Heard::Began);
                }
                None
            }
            (Phase::Waiting, Mode::Busy { barge: true }) => {
                if rms > (self.floor * 6.0).max(0.04) {
                    self.loud += 1;
                } else {
                    self.loud = 0;
                }
                if self.loud >= BARGE_FRAMES {
                    self.begin_hearing();
                    return Some(Heard::Barge);
                }
                None
            }
            (Phase::Waiting, _) => None,
            (Phase::Hearing, _) => {
                self.speech.push(frame);
                if rms < end_level {
                    self.quiet += 1;
                } else {
                    self.quiet = 0;
                    self.voiced += 1;
                }
                let seconds = self.speech.len() as f32 * FRAME_SECONDS;
                if self.quiet >= END_FRAMES || seconds >= MAX_SECONDS {
                    return Some(self.finish());
                }
                None
            }
        }
    }

    fn begin_hearing(&mut self) {
        self.speech = self.pre.iter().cloned().collect();
        self.voiced = self.loud;
        self.quiet = 0;
        self.loud = 0;
        self.phase = Phase::Hearing;
    }

    fn finish(&mut self) -> Heard {
        self.phase = Phase::Waiting;
        let keep = self.speech.len().saturating_sub(self.quiet.saturating_sub(KEEP_QUIET_FRAMES) as usize);
        let voiced = self.voiced as f32 * FRAME_SECONDS;
        let frames = std::mem::take(&mut self.speech);
        self.quiet = 0;
        self.voiced = 0;
        if voiced < MIN_VOICED_SECONDS {
            return Heard::TooShort;
        }
        Heard::Utterance(frames.into_iter().take(keep).flatten().collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const RATE: u32 = 16_000;

    fn tone(seconds: f32, amp: f32) -> Vec<f32> {
        let n = (RATE as f32 * seconds) as usize;
        (0..n).map(|i| amp * (i as f32 * 220.0 * std::f32::consts::TAU / RATE as f32).sin()).collect()
    }

    fn silence(seconds: f32) -> Vec<f32> {
        vec![0.0005; (RATE as f32 * seconds) as usize]
    }

    #[test]
    fn a_spoken_turn_is_heard_with_its_preroll_and_a_short_tail() {
        let mut vad = Vad::new(RATE);
        assert!(vad.feed(&silence(1.0), Mode::Listen).is_empty());
        let began = vad.feed(&tone(1.0, 0.2), Mode::Listen);
        assert_eq!(began, vec![Heard::Began]);
        let out = vad.feed(&silence(1.2), Mode::Listen);
        let [Heard::Utterance(samples)] = out.as_slice() else { panic!("expected an utterance, got {out:?}") };
        let seconds = samples.len() as f32 / RATE as f32;
        // 0.3 s pre-roll + 1 s of voice + at most 0.2 s of trailing quiet.
        assert!((1.2..=1.55).contains(&seconds), "utterance lasted {seconds} s");
    }

    #[test]
    fn a_cough_is_too_short() {
        let mut vad = Vad::new(RATE);
        vad.feed(&silence(0.5), Mode::Listen);
        let mut out = vad.feed(&tone(0.12, 0.2), Mode::Listen);
        out.extend(vad.feed(&silence(1.0), Mode::Listen));
        assert_eq!(out, vec![Heard::Began, Heard::TooShort]);
    }

    #[test]
    fn busy_without_talk_over_ignores_speech_and_muted_drops_it() {
        let mut vad = Vad::new(RATE);
        assert!(vad.feed(&tone(1.0, 0.3), Mode::Busy { barge: false }).is_empty());
        assert!(vad.feed(&tone(1.0, 0.3), Mode::Muted).is_empty());
        assert_eq!(vad.phase, Phase::Waiting);
    }

    #[test]
    fn talking_over_a_reply_barges_in_and_keeps_the_words() {
        let mut vad = Vad::new(RATE);
        // Quiet speech from the speakers doesn't count; a loud voice does after 160 ms.
        assert!(vad.feed(&tone(0.5, 0.02), Mode::Busy { barge: true }).is_empty());
        let out = vad.feed(&tone(0.3, 0.3), Mode::Busy { barge: true });
        assert_eq!(out, vec![Heard::Barge]);
        let mut out = vad.feed(&tone(0.6, 0.3), Mode::Listen);
        out.extend(vad.feed(&silence(1.0), Mode::Listen));
        assert!(matches!(out.as_slice(), [Heard::Utterance(_)]), "{out:?}");
    }

    #[test]
    fn the_noise_floor_follows_a_noisy_room() {
        let mut vad = Vad::new(RATE);
        // Steady fan noise just under the starting level raises the floor, so it never starts a turn.
        let noise: Vec<f32> = (0..RATE as usize * 4).map(|i| 0.011 * ((i * 7919) % 17) as f32 / 8.0 - 0.011).collect();
        assert!(vad.feed(&noise, Mode::Listen).is_empty());
        assert!(vad.floor > 0.004);
    }
}
