//! Sound in and out with cpal. The speaker plays the reply's streamed 16-bit PCM pieces back to
//! back, as the page's Web Audio player does: a session per reply, a 150 ms lead before the
//! first piece, nothing between pieces, and a way to stop at once. The microphone collects
//! mono samples for the voice detector and for voice messages. Each owns its cpal stream on a
//! thread of its own (streams aren't `Send` everywhere); the UI talks to them through shared
//! buffers and atomics.

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};

fn store_f32(a: &AtomicU32, v: f32) {
    a.store(v.to_bits(), Ordering::Relaxed);
}
fn load_f32(a: &AtomicU32) -> f32 {
    f32::from_bits(a.load(Ordering::Relaxed))
}

/// Decode little-endian signed 16-bit PCM to floats in -1..1.
pub fn pcm16_to_f32(bytes: &[u8]) -> Vec<f32> {
    bytes.chunks_exact(2).map(|b| i16::from_le_bytes([b[0], b[1]]) as f32 / 32768.0).collect()
}

/// A 16-bit mono WAV file of `samples` at `rate`, as the page's `wavFile()` writes.
pub fn wav_bytes(samples: &[f32], rate: u32) -> Vec<u8> {
    let data_len = (samples.len() * 2) as u32;
    let mut out = Vec::with_capacity(44 + data_len as usize);
    out.extend_from_slice(b"RIFF");
    out.extend_from_slice(&(36 + data_len).to_le_bytes());
    out.extend_from_slice(b"WAVEfmt ");
    out.extend_from_slice(&16u32.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes()); // PCM
    out.extend_from_slice(&1u16.to_le_bytes()); // mono
    out.extend_from_slice(&rate.to_le_bytes());
    out.extend_from_slice(&(rate * 2).to_le_bytes());
    out.extend_from_slice(&2u16.to_le_bytes());
    out.extend_from_slice(&16u16.to_le_bytes());
    out.extend_from_slice(b"data");
    out.extend_from_slice(&data_len.to_le_bytes());
    for s in samples {
        out.extend_from_slice(&(((s.clamp(-1.0, 1.0)) * 32767.0) as i16).to_le_bytes());
    }
    out
}

/// The samples and rate of a 16-bit PCM WAV (the server's saved replies), mixed to mono.
pub fn read_wav(bytes: &[u8]) -> Option<(Vec<f32>, u32)> {
    if bytes.len() < 12 || &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        return None;
    }
    let (mut rate, mut channels, mut bits) = (0u32, 0u16, 0u16);
    let mut i = 12;
    while i + 8 <= bytes.len() {
        let id = &bytes[i..i + 4];
        let len = u32::from_le_bytes(bytes[i + 4..i + 8].try_into().ok()?) as usize;
        let body = &bytes[i + 8..(i + 8 + len).min(bytes.len())];
        if id == b"fmt " && body.len() >= 16 {
            channels = u16::from_le_bytes([body[2], body[3]]);
            rate = u32::from_le_bytes(body[4..8].try_into().ok()?);
            bits = u16::from_le_bytes([body[14], body[15]]);
        } else if id == b"data" {
            if bits != 16 || channels == 0 {
                return None;
            }
            let all = pcm16_to_f32(body);
            let mono = all.chunks(channels as usize).map(|c| c.iter().sum::<f32>() / c.len() as f32).collect();
            return Some((mono, rate));
        }
        i += 8 + len + (len & 1);
    }
    None
}

struct Out {
    queue: VecDeque<f32>,
    session: u64,
    finished: bool,
    /// Resampler state: where the next output sample falls between the last two inputs.
    pos: f64,
    last: f32,
    played: u64,
}

struct SpeakerShared {
    out: Mutex<Out>,
    level: AtomicU32,
    rate: AtomicU32,
    ok: AtomicBool,
}

/// Streamed speech playback. Cheap to clone; every clone drives the same output.
#[derive(Clone)]
pub struct Speaker {
    shared: Arc<SpeakerShared>,
}

impl Speaker {
    /// Opens the default output device on a thread of its own. Without one (or headless) the
    /// speaker still takes pieces and drains them in real time, silently, so turns still end.
    fn shared() -> Arc<SpeakerShared> {
        Arc::new(SpeakerShared {
            out: Mutex::new(Out { queue: VecDeque::new(), session: 0, finished: true, pos: 0.0, last: 0.0, played: 0 }),
            level: AtomicU32::new(0),
            rate: AtomicU32::new(48_000),
            ok: AtomicBool::new(false),
        })
    }

    pub fn new(enabled: bool) -> Speaker {
        let shared = Self::shared();
        if enabled {
            let s = shared.clone();
            let (ready, wait) = mpsc::channel();
            std::thread::Builder::new()
                .name("speaker".into())
                .spawn(move || {
                    let stream = open_output(s.clone());
                    let _ = ready.send(());
                    if let Ok(stream) = stream {
                        let _ = stream.play();
                        s.ok.store(true, Ordering::Relaxed);
                        // Keep the stream alive for the life of the app.
                        loop {
                            std::thread::park();
                        }
                    } else if let Err(e) = stream {
                        eprintln!("lfm3-omni: no audio output: {e}");
                        drain_silently(s);
                    }
                })
                .expect("speaker thread");
            let _ = wait.recv_timeout(std::time::Duration::from_secs(2));
        } else {
            let s = shared.clone();
            std::thread::spawn(move || drain_silently(s));
        }
        Speaker { shared }
    }

    /// Start a new reply's playback, silencing anything still playing; returns its session.
    pub fn begin(&self) -> u64 {
        let rate = self.shared.rate.load(Ordering::Relaxed);
        let mut out = self.shared.out.lock().unwrap();
        out.session += 1;
        out.queue.clear();
        out.finished = false;
        out.pos = 0.0;
        out.last = 0.0;
        out.played = 0;
        // The page gives the first piece a 150 ms lead.
        out.queue.extend(std::iter::repeat_n(0.0, (rate as f32 * 0.15) as usize));
        out.session
    }

    /// Queue a piece of speech at `rate` for `session`; dropped if that session was stopped.
    pub fn push(&self, session: u64, samples: &[f32], rate: u32) {
        let out_rate = self.shared.rate.load(Ordering::Relaxed) as f64;
        let mut out = self.shared.out.lock().unwrap();
        if out.session != session || out.finished {
            return;
        }
        // Linear resampling, carried across pieces so they join without a seam.
        let step = rate as f64 / out_rate;
        let mut pos = out.pos;
        let mut last = out.last;
        let mut resampled = Vec::with_capacity((samples.len() as f64 / step) as usize + 2);
        for &s in samples {
            while pos < 1.0 {
                resampled.push(last + (s - last) * pos as f32);
                pos += step;
            }
            pos -= 1.0;
            last = s;
        }
        out.pos = pos;
        out.last = last;
        out.queue.extend(resampled);
    }

    /// No more pieces for `session`: it ends once what is queued has played.
    pub fn finish(&self, session: u64) {
        let mut out = self.shared.out.lock().unwrap();
        if out.session == session {
            out.finished = true;
        }
    }

    /// Stop now.
    pub fn stop(&self) {
        let mut out = self.shared.out.lock().unwrap();
        out.session += 1;
        out.queue.clear();
        out.finished = true;
    }

    /// Play a whole clip (a saved reply), as its own session.
    pub fn play_clip(&self, samples: &[f32], rate: u32) -> u64 {
        let session = self.begin();
        self.push(session, samples, rate);
        self.finish(session);
        session
    }

    /// True while `session` still has sound to play or more may come.
    pub fn is_playing(&self, session: u64) -> bool {
        let out = self.shared.out.lock().unwrap();
        out.session == session && (!out.finished || !out.queue.is_empty())
    }

    /// A smoothed 0..1 loudness of what is playing, for the orb.
    pub fn level(&self) -> f32 {
        load_f32(&self.shared.level)
    }
}

fn take_block(shared: &SpeakerShared, n: usize, mut put: impl FnMut(usize, f32)) {
    let mut sum = 0.0f32;
    {
        let mut out = shared.out.lock().unwrap();
        let mut taken = 0u64;
        for i in 0..n {
            match out.queue.pop_front() {
                Some(s) => {
                    put(i, s);
                    sum += s * s;
                    taken += 1;
                }
                None => put(i, 0.0),
            }
        }
        out.played += taken;
    }
    let rms = (sum / n.max(1) as f32).sqrt();
    let target = (rms * 5.0).min(1.0);
    let prev = load_f32(&shared.level);
    let k = if target > prev { 0.5 } else { 0.12 };
    store_f32(&shared.level, prev + (target - prev) * k);
}

fn logging() -> bool {
    std::env::var("LFM3_NATIVE_LOG").is_ok_and(|v| v == "1")
}

fn open_output(shared: Arc<SpeakerShared>) -> Result<cpal::Stream, String> {
    let host = cpal::default_host();
    let device = host.default_output_device().ok_or("no output device")?;
    let config = device.default_output_config().map_err(|e| e.to_string())?;
    if config.sample_format() != cpal::SampleFormat::F32 {
        return Err(format!("unsupported output format {:?}", config.sample_format()));
    }
    let config = config.config();
    let channels = config.channels as usize;
    if logging() {
        eprintln!("lfm3-omni: speaker open: {} Hz, {channels} channels", config.sample_rate);
    }
    shared.rate.store(config.sample_rate, Ordering::Relaxed);
    let s = shared.clone();
    device
        .build_output_stream(
            config,
            move |data: &mut [f32], _| {
                let frames = data.len() / channels.max(1);
                take_block(&s, frames, |i, v| {
                    for c in 0..channels {
                        data[i * channels + c] = v;
                    }
                });
            },
            // An underrun as the stream starts is routine; other errors are worth a line.
            |e| {
                let e = e.to_string();
                if logging() || !e.contains("underrun") {
                    eprintln!("lfm3-omni: output stream error: {e}");
                }
            },
            None,
        )
        .map_err(|e| e.to_string())
}

fn drain_silently(shared: Arc<SpeakerShared>) {
    // Paced by the clock rather than by the sleeps, which run long, so playback ends when it
    // would on a real device.
    let rate = shared.rate.load(Ordering::Relaxed) as f64;
    let start = std::time::Instant::now();
    let mut taken = 0u64;
    loop {
        std::thread::sleep(std::time::Duration::from_millis(20));
        let due = (start.elapsed().as_secs_f64() * rate) as u64;
        take_block(&shared, (due - taken) as usize, |_, _| {});
        taken = due;
    }
}

struct MicShared {
    samples: Mutex<Vec<f32>>,
    rate: AtomicU32,
    running: AtomicBool,
    generation: AtomicU64,
    error: Mutex<Option<String>>,
}

/// The default microphone, mixed to mono. Open only while live mode or a voice message needs
/// it, so the system's microphone indicator is off the rest of the time.
#[derive(Clone)]
pub struct Mic {
    shared: Arc<MicShared>,
    enabled: bool,
}

impl Mic {
    pub fn new(enabled: bool) -> Mic {
        Mic {
            shared: Arc::new(MicShared {
                samples: Mutex::new(Vec::new()),
                rate: AtomicU32::new(48_000),
                running: AtomicBool::new(false),
                generation: AtomicU64::new(0),
                error: Mutex::new(None),
            }),
            enabled,
        }
    }

    pub fn is_running(&self) -> bool {
        self.shared.running.load(Ordering::Relaxed)
    }

    pub fn rate(&self) -> u32 {
        self.shared.rate.load(Ordering::Relaxed)
    }

    pub fn error(&self) -> Option<String> {
        self.shared.error.lock().unwrap().clone()
    }

    /// Open the microphone (no-op if open). Returns its sample rate.
    pub fn start(&self) -> u32 {
        if self.is_running() || !self.enabled {
            return self.rate();
        }
        self.shared.samples.lock().unwrap().clear();
        *self.shared.error.lock().unwrap() = None;
        self.shared.running.store(true, Ordering::Relaxed);
        let generation = self.shared.generation.fetch_add(1, Ordering::Relaxed) + 1;
        let shared = self.shared.clone();
        let (ready, wait) = mpsc::channel();
        std::thread::Builder::new()
            .name("mic".into())
            .spawn(move || {
                match open_input(shared.clone()) {
                    Ok(stream) => {
                        let _ = stream.play();
                        let _ = ready.send(());
                        while shared.running.load(Ordering::Relaxed) && shared.generation.load(Ordering::Relaxed) == generation {
                            std::thread::sleep(std::time::Duration::from_millis(30));
                        }
                        drop(stream);
                    }
                    Err(e) => {
                        *shared.error.lock().unwrap() = Some(e);
                        shared.running.store(false, Ordering::Relaxed);
                        let _ = ready.send(());
                    }
                }
            })
            .expect("mic thread");
        let _ = wait.recv_timeout(std::time::Duration::from_secs(3));
        self.rate()
    }

    pub fn stop(&self) {
        self.shared.running.store(false, Ordering::Relaxed);
        self.shared.samples.lock().unwrap().clear();
    }

    /// Everything captured since the last call.
    pub fn take(&self) -> Vec<f32> {
        std::mem::take(&mut *self.shared.samples.lock().unwrap())
    }

    /// Feed samples as if heard (tests and the `--hear` flag).
    pub fn inject(&self, samples: &[f32], rate: u32) {
        self.shared.rate.store(rate, Ordering::Relaxed);
        self.shared.samples.lock().unwrap().extend_from_slice(samples);
    }
}

fn open_input(shared: Arc<MicShared>) -> Result<cpal::Stream, String> {
    let host = cpal::default_host();
    let device = host.default_input_device().ok_or("No microphone was found.")?;
    let supported = device.default_input_config().map_err(|e| e.to_string())?;
    let format = supported.sample_format();
    let config = supported.config();
    let channels = config.channels as usize;
    if logging() {
        eprintln!("lfm3-omni: microphone open: {} Hz, {channels} channels, {format:?}", config.sample_rate);
    }
    shared.rate.store(config.sample_rate, Ordering::Relaxed);
    let err = |e: cpal::Error| eprintln!("lfm3-omni: input stream error: {e}");
    let push = move |shared: &MicShared, mono: &mut dyn Iterator<Item = f32>| {
        shared.samples.lock().unwrap().extend(mono);
    };
    match format {
        cpal::SampleFormat::F32 => {
            let s = shared.clone();
            device
                .build_input_stream(
                    config,
                    move |data: &[f32], _| {
                        push(&s, &mut data.chunks(channels.max(1)).map(|c| c.iter().sum::<f32>() / c.len() as f32));
                    },
                    err,
                    None,
                )
                .map_err(|e| e.to_string())
        }
        cpal::SampleFormat::I16 => {
            let s = shared.clone();
            device
                .build_input_stream(
                    config,
                    move |data: &[i16], _| {
                        push(&s, &mut data.chunks(channels.max(1)).map(|c| c.iter().map(|&v| v as f32 / 32768.0).sum::<f32>() / c.len() as f32));
                    },
                    err,
                    None,
                )
                .map_err(|e| e.to_string())
        }
        other => Err(format!("unsupported microphone format {other:?}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wav_round_trips() {
        let samples: Vec<f32> = (0..480).map(|i| (i as f32 / 40.0).sin() * 0.5).collect();
        let (back, rate) = read_wav(&wav_bytes(&samples, 24_000)).unwrap();
        assert_eq!(rate, 24_000);
        assert_eq!(back.len(), samples.len());
        assert!(back.iter().zip(&samples).all(|(a, b)| (a - b).abs() < 1e-3));
    }

    #[test]
    fn pieces_play_back_to_back_and_a_stop_drops_the_rest() {
        let speaker = Speaker { shared: Speaker::shared() };
        let rate = speaker.shared.rate.load(Ordering::Relaxed) as usize;
        let session = speaker.begin();
        let lead = speaker.shared.out.lock().unwrap().queue.len();
        assert_eq!(lead, rate * 15 / 100);
        speaker.push(session, &vec![0.25; 2400], 24_000);
        speaker.push(session, &vec![0.25; 2400], 24_000);
        let queued = speaker.shared.out.lock().unwrap().queue.len() - lead;
        // 0.2 s at 24 kHz becomes 0.2 s at the output rate, give or take a sample.
        assert!((queued as i64 - (rate / 5) as i64).abs() <= 2, "{queued}");
        assert!(speaker.is_playing(session));
        speaker.stop();
        assert!(!speaker.is_playing(session));
        speaker.push(session, &[0.5; 100], 24_000);
        assert!(speaker.shared.out.lock().unwrap().queue.is_empty(), "a stopped session takes no more pieces");
    }

    /// The real output device: one second of near-silent 24 kHz audio plays out in about a
    /// second, then the session ends. Run with `cargo test -- --ignored`.
    #[test]
    #[ignore]
    fn the_default_output_plays_a_reply_in_real_time() {
        let speaker = Speaker::new(true);
        let session = speaker.begin();
        let started = std::time::Instant::now();
        let quiet: Vec<f32> = (0..24_000).map(|i| 0.0005 * (i as f32 * 0.1).sin()).collect();
        for piece in quiet.chunks(4_800) {
            speaker.push(session, piece, 24_000);
        }
        speaker.finish(session);
        while speaker.is_playing(session) && started.elapsed().as_secs_f32() < 5.0 {
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        let took = started.elapsed().as_secs_f32();
        // 0.15 s of lead silence + 1 s of audio, plus the device's buffering.
        assert!((1.1..1.6).contains(&took), "playback took {took} s (rate {})", speaker.shared.rate.load(Ordering::Relaxed));
    }
}
