//! LFM3-Omni: a native macOS window for LFM3-Omni Lab, drawn with Rune.
//!
//! It talks to the lab server (`./start.sh`, port 8765) exactly as the web page does: model
//! status and control, media uploads, the streamed chat with tools, spoken replies played as
//! they arrive, voice messages, and a live voice mode with the page's voice detector.
//!
//! Flags (after Rune's own):
//!   --server URL   the lab server (default http://127.0.0.1:8765, or $LFM3_SERVER)
//!   --live         open in live mode
//!   --hear FILE    live mode hears this WAV instead of the microphone (testing)
//! LFM3_LAB_DIR points at the lab folder (default: the checkout this was built from).
//! Headless tooling (`--snap`, `--script`, ...) draws the scenes below without network unless
//! LFM3_NATIVE_NET=1; LFM3_NATIVE_REALTIME=1 holds the virtual clock to the wall clock so a
//! scripted headless run can wait for real answers.

mod api;
mod audio;
mod live_view;
mod look;
mod markdown;
mod model;
mod orb;
mod prefs;
mod scenes;
mod vad;
mod view;

use model::State;
use rune::{AppSpec, Key, KeyEvent, Tx, Waker};
use std::path::PathBuf;
use std::sync::{Arc, OnceLock};

struct Flags {
    server: String,
    live: bool,
    hear: Option<PathBuf>,
}

fn flags() -> &'static Flags {
    static FLAGS: OnceLock<Flags> = OnceLock::new();
    FLAGS.get_or_init(|| {
        let args = rune::extra_args();
        let mut f = Flags { server: std::env::var("LFM3_SERVER").unwrap_or_else(|_| "http://127.0.0.1:8765".into()), live: false, hear: None };
        let mut i = 0;
        while i < args.len() {
            match args[i].as_str() {
                "--server" if i + 1 < args.len() => {
                    f.server = args[i + 1].clone();
                    i += 1;
                }
                "--live" => f.live = true,
                "--hear" if i + 1 < args.len() => {
                    f.hear = Some(PathBuf::from(&args[i + 1]));
                    i += 1;
                }
                _ => {}
            }
            i += 1;
        }
        f
    })
}

fn headless() -> bool {
    rune::tooling::is_headless()
}

fn env_on(name: &str) -> bool {
    std::env::var(name).is_ok_and(|v| v == "1")
}

fn init() -> State {
    let f = flags();
    // The lab folder (for "Start lab server" and the sample files): $LFM3_LAB_DIR, else the
    // checkout this app was built from.
    let lab_dir = std::env::var_os("LFM3_LAB_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().map(PathBuf::from).unwrap_or_default());
    let net = !headless() || env_on("LFM3_NATIVE_NET");
    let sound = !headless() && f.hear.is_none();
    let mut s = State::new(&f.server, lab_dir, net, sound);
    if let Some(path) = &f.hear {
        match std::fs::read(path).ok().and_then(|b| audio::read_wav(&b)) {
            Some((samples, rate)) => {
                s.mic.inject(&[], rate);
                s.hear = Some((samples, rate));
            }
            None => eprintln!("lfm3-omni: --hear needs a 16-bit WAV file: {}", path.display()),
        }
    }
    s
}

fn setup(s: &mut State, waker: Waker) {
    s.waker = waker.clone();
    s.images = Some(Arc::new(rune::ImageCache::new(waker)));
    s.start_polling();
}

fn poll(s: &mut State, t: f64, tx: &mut Tx) -> bool {
    if headless() && env_on("LFM3_NATIVE_REALTIME") {
        // Let real answers arrive in a scripted headless run: frame time follows wall time.
        static START: OnceLock<std::time::Instant> = OnceLock::new();
        let start = *START.get_or_init(std::time::Instant::now);
        let ahead = t - start.elapsed().as_secs_f64();
        if ahead > 0.0 {
            std::thread::sleep(std::time::Duration::from_secs_f64(ahead.min(0.5)));
        }
    }
    if flags().live && s.live.is_none() && s.ready() && !s.opened_live {
        s.opened_live = true;
        s.enter_live();
    }
    s.poll(t, tx)
}

fn on_key(s: &mut State, ev: &KeyEvent, tx: &mut Tx) -> bool {
    match &ev.key {
        Key::Escape => {
            if s.live.is_some() {
                s.exit_live();
            } else if s.recording.is_some() {
                s.cancel_recording();
            } else if s.turn.is_some() || s.playing.is_some() {
                s.stop_reply();
            } else {
                return false;
            }
            true
        }
        Key::Command('n') => {
            s.new_chat();
            true
        }
        Key::Command('l') => {
            if s.live.is_some() {
                s.exit_live();
            } else {
                s.enter_live();
            }
            true
        }
        _ => {
            let _ = tx;
            false
        }
    }
}

fn on_quit(s: &mut State, _: &mut Tx) {
    s.mic.stop();
    s.speaker.stop();
    s.stop_reply();
    s.stop_launched_server();
}

/// SF Pro from the system, when it's there (it isn't redistributed: it is read at launch).
fn system_fonts() -> (&'static [&'static [u8]], (&'static str, &'static str)) {
    let read = |p: &str| std::fs::read(p).ok();
    match (read("/System/Library/Fonts/SFNS.ttf"), read("/System/Library/Fonts/SFNSMono.ttf")) {
        (Some(sans), Some(mono)) => {
            let faces: Vec<&'static [u8]> = vec![Box::leak(sans.into_boxed_slice()), Box::leak(mono.into_boxed_slice())];
            (Box::leak(faces.into_boxed_slice()), ("System Font", ".SF NS Mono"))
        }
        _ => (&[], ("Inter", "Inter")),
    }
}

fn main() {
    rune::prewarm_gpu();
    let (fonts, families) = system_fonts();
    let spec = AppSpec::<State> {
        min_size: Some((620.0, 560.0)),
        fonts,
        families,
        setup: Some(setup),
        poll: Some(poll),
        on_key: Some(on_key),
        scenes: scenes::SCENES,
        ..AppSpec::new("LFM3-Omni", (900.0, 760.0), init, view::view)
    };
    let mut app = rune::App::new(spec).standard_menus().on_quit(on_quit);
    let args: Vec<String> = std::env::args().collect();
    if args.windows(2).any(|w| w[0] == "--scene" && w[1] == "icon") {
        // The icon renders with transparent corners.
        app = app.window_options(rune::WindowOptions { transparent: true, ..Default::default() });
    }
    app.run();
}
