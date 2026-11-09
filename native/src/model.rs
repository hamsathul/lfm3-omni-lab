//! The app's state and everything that changes it: turns streamed from the server, the
//! conversation, attachments, voice messages, playback and live mode. Worker threads send
//! `Ev`s through a channel and wake the window; `poll` applies them on the UI thread.

use crate::api::{self, Api, Media, ModelStatus};
use crate::audio::{self, Mic, Speaker};
use crate::vad::{Heard, Mode, Vad};
use base64::Engine;
use rune::{Image, ImageCache, TextEdit, Tx, Waker};
use serde_json::Value;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{Receiver, Sender, channel};
use std::sync::Arc;

pub const WEB_TOOLS: [&str; 5] = ["web_search", "open_page", "click", "type_text", "go_back"];

/// Seconds the server must stay unreachable before the offline card shows.
pub const OFFLINE_GRACE: f64 = 1.2;

pub enum Ev {
    Status(Result<ModelStatus, String>),
    ModelAction(Result<ModelStatus, String>),
    Turn(u64, TurnEv),
    Picture { msg: u64, step: usize, bytes: Vec<u8> },
    Replay { msg: u64, clip: Result<(Vec<f32>, u32), String> },
    Sample { name: String, bytes: Result<Vec<u8>, String>, starter: usize },
    /// "Set up the lab" finished downloading it, or couldn't.
    LabFetched(Result<PathBuf, String>),
}

pub enum TurnEv {
    Uploaded(Vec<Media>),
    Started { rid: String, speak: bool },
    Transcript(String),
    Token(String),
    Audio(u64),
    ToolCall { name: String, args: Value },
    ToolResult { name: String, result: Value, screenshot: Option<String> },
    Trimmed(i64),
    Done { text: String, audio: Option<String>, stats: Value, append: Vec<Value> },
    Failed(String),
}

#[derive(Clone)]
pub enum Source {
    File(PathBuf),
    Bytes(Arc<Vec<u8>>),
}

#[derive(Clone)]
pub struct Item {
    pub name: String,
    pub kind: String,
    pub voice: bool,
    pub seconds: Option<f64>,
    pub image: Option<Image>,
    pub source: Source,
}

pub struct UserMsg {
    pub text: String,
    pub items: Vec<Item>,
    pub transcript: Option<String>,
    pub live: bool,
}

pub struct ToolStep {
    pub name: String,
    pub args: Value,
    pub result: Option<Value>,
    pub picture: Option<Image>,
}

pub enum Seg {
    Text(String),
    Tool(ToolStep),
}

#[derive(Clone, PartialEq)]
pub enum BotState {
    Waiting,
    Streaming,
    Done,
    Cancelled,
    Failed(String),
}

#[derive(Clone)]
pub struct Retry {
    pub text: String,
    pub items: Vec<Item>,
    pub media: Option<Vec<String>>,
    pub reply: String,
    pub tools: bool,
    pub web: bool,
}

pub struct BotMsg {
    pub segs: Vec<Seg>,
    pub state: BotState,
    pub stats: Option<Value>,
    pub audio: Option<String>,
    pub speak: bool,
    pub retry: Option<Retry>,
}

pub enum Body {
    User(UserMsg),
    Bot(BotMsg),
    Note(String),
}

pub struct Msg {
    pub id: u64,
    /// Bumped on every change, so settled rows can be memoised.
    pub version: u64,
    pub body: Body,
}

pub struct Turn {
    pub id: u64,
    pub user: u64,
    pub bot: u64,
    pub rid: Option<String>,
    pub stop: Arc<AtomicBool>,
    pub session: Option<u64>,
    pub live: bool,
    pub retry: Retry,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Reply {
    Auto,
    Text,
    Voice,
}

impl Reply {
    pub fn wire(self) -> &'static str {
        match self {
            Reply::Auto => "auto",
            Reply::Text => "text",
            Reply::Voice => "voice",
        }
    }
}

pub struct Recording {
    pub started: f64,
    pub samples: Vec<f32>,
    pub rate: u32,
    pub level: f32,
}

#[derive(Clone, PartialEq, Debug)]
pub enum LivePhase {
    Listening,
    Hearing,
    Sending,
    Thinking,
    Searching,
    Reading,
    Speaking,
    Saved,
    Error(String),
}

pub struct Live {
    pub phase: LivePhase,
    pub vad: Vad,
    pub heard: String,
    pub caption: String,
    pub muted: bool,
    pub barge: bool,
    pub level: f32,
    pub next: Option<(f64, LivePhase)>,
    pub speech_end: Option<f64>,
    pub answered_in: Option<f64>,
    pub stats: Option<Value>,
    pub pending: Option<Vec<f32>>,
    pub saved: bool,
}

impl Live {
    pub fn mood(&self) -> crate::orb::Mood {
        use crate::orb::Mood;
        if self.muted && matches!(self.phase, LivePhase::Listening) {
            return Mood::Muted;
        }
        match self.phase {
            LivePhase::Listening => Mood::Listening,
            LivePhase::Hearing => Mood::Hearing,
            LivePhase::Sending | LivePhase::Thinking => Mood::Thinking,
            LivePhase::Searching | LivePhase::Reading => Mood::Searching,
            LivePhase::Speaking | LivePhase::Saved => Mood::Speaking,
            LivePhase::Error(_) => Mood::Error,
        }
    }

    pub fn label(&self) -> String {
        match &self.phase {
            LivePhase::Listening if self.muted => "Muted".into(),
            LivePhase::Listening => "Listening".into(),
            LivePhase::Hearing => "Hearing you".into(),
            LivePhase::Sending => "Got it".into(),
            LivePhase::Thinking => "Thinking".into(),
            LivePhase::Searching => "Searching the web".into(),
            LivePhase::Reading => "Reading the page".into(),
            LivePhase::Speaking => "Speaking".into(),
            LivePhase::Saved => "Saved".into(),
            LivePhase::Error(e) => e.clone(),
        }
    }
}

pub struct Starter {
    pub label: &'static str,
    pub text: &'static str,
    pub sample: Option<&'static str>,
    pub reply: Option<Reply>,
    pub web: bool,
}

/// The page's starter prompts (static/app.js), samples served from /samples.
pub const STARTERS: [Starter; 6] = [
    Starter { label: "Read a sign", text: "What does this sign say?", sample: Some("/samples/sign.png"), reply: None, web: false },
    Starter { label: "Time in Tokyo", text: "What time is it in Tokyo right now?", sample: None, reply: None, web: false },
    Starter { label: "Transcribe a clip", text: "Transcribe this recording word for word.", sample: Some("/samples/speech.wav"), reply: Some(Reply::Text), web: false },
    Starter { label: "Describe a video", text: "Describe what happens in this clip and what you can hear.", sample: Some("/samples/clip.mp4"), reply: None, web: false },
    Starter { label: "Latest Python", text: "Look up the latest Python release.", sample: None, reply: Some(Reply::Text), web: true },
    Starter { label: "Read aloud", text: "Read this aloud exactly: Everything you hear is generated on this Mac, with no internet connection.", sample: None, reply: Some(Reply::Voice), web: false },
];

pub enum Server {
    /// Not asked yet.
    Unknown,
    Up(ModelStatus),
    /// Unreachable since this poll time.
    Down { since: f64, error: String },
}

pub struct State {
    pub api: Api,
    /// The lab folder, for "Start lab server"; None when this app can't find one.
    pub lab_dir: Option<PathBuf>,
    /// Why the lab couldn't be set up, chosen or started, for the offline card.
    pub lab_problem: Option<String>,
    pub fetching_lab: bool,
    /// The newest line of the starting server's output (installing, downloading the model).
    pub launch_line: Option<String>,
    launch_read_at: f64,
    pub net: bool,
    pub events: Receiver<Ev>,
    pub sender: Sender<Ev>,
    pub waker: Waker,
    pub images: Option<Arc<ImageCache>>,
    pub speaker: Speaker,
    pub mic: Mic,
    pub server: Server,
    pub launched: Option<std::process::Child>,
    pub launching_at: Option<f64>,
    pub model_action: Option<String>,
    pub messages: Vec<Msg>,
    pub history: Vec<Value>,
    pub draft: TextEdit,
    pub staged: Vec<Item>,
    pub reply: Reply,
    pub tools: bool,
    pub web: bool,
    pub turn: Option<Turn>,
    pub playing: Option<(u64, u64)>,
    pub loading_replay: Option<u64>,
    pub recording: Option<Recording>,
    pub live: Option<Live>,
    pub drop_hover: bool,
    pub now: f64,
    /// `--hear`: a recording live mode hears once instead of the microphone.
    pub hear: Option<(Vec<f32>, u32)>,
    pub opened_live: bool,
    /// The `icon` scene: draw the app icon instead of the window.
    pub icon: bool,
    next_id: u64,
}

impl State {
    pub fn new(base: &str, lab_dir: Option<PathBuf>, net: bool, sound: bool) -> State {
        let (sender, events) = channel();
        let prefs = crate::prefs::load();
        State {
            api: Api::new(base),
            lab_dir,
            lab_problem: None,
            fetching_lab: false,
            launch_line: None,
            launch_read_at: 0.0,
            net,
            events,
            sender,
            waker: Arc::new(|| {}),
            images: None,
            speaker: Speaker::new(sound),
            mic: Mic::new(sound),
            server: Server::Unknown,
            launched: None,
            launching_at: None,
            model_action: None,
            messages: Vec::new(),
            history: Vec::new(),
            draft: TextEdit::new(""),
            staged: Vec::new(),
            reply: prefs.reply,
            tools: prefs.tools,
            web: prefs.web,
            turn: None,
            playing: None,
            loading_replay: None,
            recording: None,
            live: None,
            drop_hover: false,
            now: 0.0,
            hear: None,
            opened_live: false,
            icon: false,
            next_id: 1,
        }
    }

    fn id(&mut self) -> u64 {
        self.next_id += 1;
        self.next_id
    }

    pub fn status(&self) -> Option<&ModelStatus> {
        match &self.server {
            Server::Up(s) => Some(s),
            _ => None,
        }
    }

    pub fn ready(&self) -> bool {
        self.status().is_some_and(ModelStatus::ready)
    }

    pub fn save_prefs(&self) {
        crate::prefs::save(&crate::prefs::Prefs { reply: self.reply, tools: self.tools, web: self.web });
    }

    fn msg_mut(&mut self, id: u64) -> Option<&mut Msg> {
        self.messages.iter_mut().find(|m| m.id == id)
    }

    fn bot_mut(&mut self, id: u64) -> Option<&mut BotMsg> {
        let m = self.msg_mut(id)?;
        m.version += 1;
        match &mut m.body {
            Body::Bot(b) => Some(b),
            _ => None,
        }
    }

    fn user_mut(&mut self, id: u64) -> Option<&mut UserMsg> {
        let m = self.msg_mut(id)?;
        m.version += 1;
        match &mut m.body {
            Body::User(u) => Some(u),
            _ => None,
        }
    }

    pub fn note(&mut self, text: impl Into<String>) {
        let id = self.id();
        self.messages.push(Msg { id, version: 0, body: Body::Note(text.into()) });
    }

    // ---- background work -------------------------------------------------------------

    /// Start the status poller: `GET /api/model` every second, sending only changes.
    pub fn start_polling(&self) {
        if !self.net {
            return;
        }
        let (api, sender, waker) = (self.api.clone(), self.sender.clone(), self.waker.clone());
        std::thread::Builder::new()
            .name("status".into())
            .spawn(move || {
                let mut last: Option<Result<ModelStatus, String>> = None;
                loop {
                    let now = api.status();
                    let same = match (&last, &now) {
                        (Some(Ok(a)), Ok(b)) => a == b,
                        (Some(Err(_)), Err(_)) => true,
                        _ => false,
                    };
                    if !same {
                        last = Some(now.clone());
                        if sender.send(Ev::Status(now)).is_err() {
                            return;
                        }
                        waker();
                    }
                    std::thread::sleep(std::time::Duration::from_millis(1000));
                }
            })
            .expect("status thread");
    }

    pub fn model_action(&mut self, action: &str) {
        if self.model_action.is_some() {
            return;
        }
        self.model_action = Some(action.to_string());
        let (api, sender, waker, action) = (self.api.clone(), self.sender.clone(), self.waker.clone(), action.to_string());
        std::thread::spawn(move || {
            let _ = sender.send(Ev::ModelAction(api.model(&action)));
            waker();
        });
    }

    /// Start the lab server (its start script, without opening the browser) when none is running.
    pub fn launch_server(&mut self) {
        if self.launched.is_some() {
            return;
        }
        let Some(lab) = self.lab_dir.clone() else { return };
        let port = self.api.base.rsplit(':').next().and_then(|p| p.parse::<u16>().ok()).unwrap_or(8765);
        self.lab_problem = None;
        self.launch_line = None;
        match crate::setup::start(&lab, port) {
            Ok(child) => {
                self.launched = Some(child);
                self.launching_at = Some(self.now);
            }
            Err(e) => self.lab_problem = Some(format!("Couldn't start the lab server: {e}")),
        }
    }

    /// Stop a server this app started.
    pub fn stop_launched_server(&mut self) {
        if let Some(mut child) = self.launched.take() {
            crate::setup::stop(&mut child);
        }
    }

    /// Use a lab folder picked in the app, and start it.
    pub fn choose_lab(&mut self, dir: PathBuf) {
        if crate::lab::is_lab(&dir) {
            crate::prefs::save_lab_dir(&dir);
            self.lab_dir = Some(dir);
            self.launch_server();
        } else {
            self.lab_problem = Some(format!(
                "{} isn't the lab: it has no {}. Choose the folder you cloned or downloaded from GitHub.",
                dir.display(),
                crate::lab::START
            ));
        }
    }

    /// Download the lab into ~/lfm3-omni-lab, then start it (which installs it and gets the model).
    pub fn fetch_lab(&mut self) {
        if self.fetching_lab {
            return;
        }
        let Some(dest) = crate::setup::default_dir() else {
            self.lab_problem = Some("There's no home folder to put the lab in.".into());
            return;
        };
        self.fetching_lab = true;
        self.lab_problem = None;
        let (sender, waker) = (self.sender.clone(), self.waker.clone());
        std::thread::spawn(move || {
            let _ = sender.send(Ev::LabFetched(crate::setup::fetch(&dest)));
            waker();
        });
    }

    /// While a server this app started is starting: follow its output, and notice if it quits.
    fn watch_launch(&mut self, now: f64) {
        let Some(child) = &mut self.launched else { return };
        let exited = matches!(child.try_wait(), Ok(Some(_)));
        let starting = self.launching_at.is_some();
        if !starting && !exited {
            return;
        }
        if exited || now - self.launch_read_at >= 0.4 {
            self.launch_read_at = now;
            self.launch_line = self.lab_dir.as_deref().and_then(|lab| crate::setup::last_line(&crate::setup::log_path(lab)));
        }
        if exited {
            self.launched = None;
            if starting {
                self.launching_at = None;
                let said = self.launch_line.take().unwrap_or_else(|| "it stopped without saying why".into());
                self.lab_problem = Some(format!("The lab server stopped: {said}"));
            }
        }
    }

    // ---- sending -----------------------------------------------------------------------

    pub fn attach_paths(&mut self, paths: &[PathBuf]) {
        for path in paths {
            let name = path.file_name().map_or("file".into(), |n| n.to_string_lossy().into_owned());
            let kind = api::kind_of(&name).to_string();
            let image = if kind == "image" { self.images.as_ref().map(|c| c.file(path, 240.0, 2.0)) } else { None };
            self.staged.push(Item { name, kind, voice: false, seconds: None, image, source: Source::File(path.clone()) });
        }
    }

    pub fn can_send(&self) -> bool {
        self.turn.is_none() && self.ready()
    }

    /// Send what is typed and staged.
    pub fn send_draft(&mut self, tx: &mut Tx) {
        let text = self.draft.text.trim().to_string();
        if text.is_empty() && self.staged.is_empty() {
            return;
        }
        if !self.can_send() {
            return;
        }
        let items = std::mem::take(&mut self.staged);
        self.draft.clear();
        let (reply, tools, web) = (self.reply.wire().to_string(), self.tools, self.web);
        self.send(text, items, reply, tools, web, false, tx);
    }

    #[allow(clippy::too_many_arguments)]
    pub fn send(&mut self, text: String, items: Vec<Item>, reply: String, tools: bool, web: bool, live: bool, tx: &mut Tx) {
        let user = self.id();
        self.messages.push(Msg { id: user, version: 0, body: Body::User(UserMsg { text: text.clone(), items: items.clone(), transcript: None, live }) });
        let retry = Retry { text, items, media: None, reply, tools, web };
        self.start_turn(user, retry, live, tx);
    }

    fn start_turn(&mut self, user: u64, retry: Retry, live: bool, tx: &mut Tx) {
        let bot = self.id();
        self.messages.push(Msg {
            id: bot,
            version: 0,
            body: Body::Bot(BotMsg { segs: Vec::new(), state: BotState::Waiting, stats: None, audio: None, speak: false, retry: None }),
        });
        let id = self.id();
        let stop = Arc::new(AtomicBool::new(false));
        self.turn = Some(Turn { id, user, bot, rid: None, stop: stop.clone(), session: None, live, retry: retry.clone() });
        tx.scroll_to_bottom("thread");
        if !self.net {
            return;
        }
        let history = self.history.clone();
        let (api, speaker, sender, waker) = (self.api.clone(), self.speaker.clone(), self.sender.clone(), self.waker.clone());
        std::thread::Builder::new()
            .name("turn".into())
            .spawn(move || run_turn(id, api, speaker, history, retry, stop, &sender, &waker))
            .expect("turn thread");
    }

    pub fn retry(&mut self, bot: u64, tx: &mut Tx) {
        if !self.can_send() {
            return;
        }
        let Some(pos) = self.messages.iter().position(|m| m.id == bot) else { return };
        let Body::Bot(b) = &self.messages[pos].body else { return };
        let Some(retry) = b.retry.clone() else { return };
        // The user message before it stays; the failed answer is replaced.
        let user = self.messages[..pos].iter().rev().find(|m| matches!(m.body, Body::User(_))).map(|m| m.id);
        self.messages.remove(pos);
        if let Some(user) = user {
            self.start_turn(user, retry, false, tx);
        }
    }

    /// Stop the reply: silence it, and cancel the turn on the server (its `done` still comes).
    pub fn stop_reply(&mut self) {
        self.speaker.stop();
        self.playing = None;
        if let Some(turn) = &self.turn {
            turn.stop.store(true, Ordering::Relaxed);
            if let Some(rid) = turn.rid.clone() {
                let api = self.api.clone();
                std::thread::spawn(move || api.cancel(&rid));
            }
        }
    }

    pub fn new_chat(&mut self) {
        self.stop_reply();
        self.messages.clear();
        self.history.clear();
        self.staged.clear();
        self.draft.clear();
    }

    pub fn starter(&mut self, i: usize, tx: &mut Tx) {
        let s = &STARTERS[i];
        if !self.can_send() {
            return;
        }
        if let Some(path) = s.sample {
            let (api, sender, waker, path) = (self.api.clone(), self.sender.clone(), self.waker.clone(), path.to_string());
            let name = path.rsplit('/').next().unwrap_or("sample").to_string();
            std::thread::spawn(move || {
                let _ = sender.send(Ev::Sample { name, bytes: api.get(&path), starter: i });
                waker();
            });
        } else {
            self.send_starter(i, Vec::new(), tx);
        }
    }

    fn send_starter(&mut self, i: usize, items: Vec<Item>, tx: &mut Tx) {
        let s = &STARTERS[i];
        let reply = s.reply.unwrap_or(self.reply).wire().to_string();
        let web = s.web || self.web;
        let tools = self.tools || s.web;
        self.send(s.text.to_string(), items, reply, tools, web, false, tx);
    }

    pub fn replay(&mut self, msg: u64) {
        if self.playing.is_some_and(|(m, _)| m == msg) {
            self.speaker.stop();
            self.playing = None;
            return;
        }
        let Some(Body::Bot(b)) = self.messages.iter().find(|m| m.id == msg).map(|m| &m.body) else { return };
        let Some(file) = b.audio.clone() else { return };
        self.loading_replay = Some(msg);
        let (api, sender, waker) = (self.api.clone(), self.sender.clone(), self.waker.clone());
        std::thread::spawn(move || {
            let clip = api.get(&format!("/outputs/{file}")).and_then(|b| audio::read_wav(&b).ok_or_else(|| "The saved reply isn't a WAV file.".to_string()));
            let _ = sender.send(Ev::Replay { msg, clip });
            waker();
        });
    }

    // ---- voice messages --------------------------------------------------------------

    pub fn start_recording(&mut self) {
        if self.recording.is_some() || self.live.is_some() {
            return;
        }
        self.speaker.stop();
        let rate = self.mic.start();
        if let Some(e) = self.mic.error() {
            self.note(e);
            return;
        }
        self.recording = Some(Recording { started: self.now, samples: Vec::new(), rate, level: 0.0 });
    }

    pub fn cancel_recording(&mut self) {
        self.recording = None;
        self.mic.stop();
    }

    /// Stop and send the voice message with whatever is typed and staged, as the page does.
    pub fn finish_recording(&mut self, tx: &mut Tx) {
        let Some(mut rec) = self.recording.take() else { return };
        rec.samples.extend(self.mic.take());
        self.mic.stop();
        let seconds = rec.samples.len() as f64 / rec.rate as f64;
        if seconds < 0.6 {
            self.note("That recording was too short to send.");
            return;
        }
        if !self.can_send() {
            self.note("The model isn't ready, so the recording wasn't sent.");
            return;
        }
        let wav = audio::wav_bytes(&rec.samples, rec.rate);
        let item = Item { name: "voice-message.wav".into(), kind: "audio".into(), voice: true, seconds: Some(seconds), image: None, source: Source::Bytes(Arc::new(wav)) };
        let mut items = std::mem::take(&mut self.staged);
        items.push(item);
        let text = self.draft.text.trim().to_string();
        self.draft.clear();
        let (reply, tools, web) = (self.reply.wire().to_string(), self.tools, self.web);
        self.send(text, items, reply, tools, web, false, tx);
    }

    // ---- live mode -------------------------------------------------------------------

    pub fn enter_live(&mut self) {
        if self.live.is_some() {
            return;
        }
        self.cancel_recording();
        let rate = self.mic.start();
        let error = self.mic.error();
        let mut live = Live {
            phase: LivePhase::Listening,
            vad: Vad::new(rate),
            heard: String::new(),
            caption: String::new(),
            muted: false,
            barge: crate::prefs::load().barge,
            level: 0.0,
            next: None,
            speech_end: None,
            answered_in: None,
            stats: None,
            pending: None,
            saved: false,
        };
        if let Some(e) = error {
            live.phase = LivePhase::Error(e);
        } else if !self.ready() {
            live.phase = LivePhase::Error("Start the model first.".into());
        }
        if self.turn.is_some() {
            live.phase = LivePhase::Thinking;
        }
        self.live = Some(live);
    }

    pub fn exit_live(&mut self) {
        if self.live.take().is_some() {
            self.mic.stop();
            if self.turn.as_ref().is_some_and(|t| t.live) {
                self.stop_reply();
            }
        }
    }

    pub fn interrupt(&mut self) {
        self.stop_reply();
        if let Some(live) = &mut self.live {
            live.vad.reset();
            live.phase = LivePhase::Listening;
            live.next = None;
        }
    }

    fn live_utterance(&mut self, samples: Vec<f32>, tx: &mut Tx) {
        let Some(live) = &mut self.live else { return };
        live.speech_end = Some(self.now);
        live.answered_in = None;
        live.heard.clear();
        live.caption.clear();
        if self.turn.is_some() {
            // A talk-over: send once the cancelled turn's `done` lands.
            live.pending = Some(samples);
            live.phase = LivePhase::Sending;
            return;
        }
        live.phase = LivePhase::Sending;
        self.send_live(samples, tx);
    }

    fn send_live(&mut self, samples: Vec<f32>, tx: &mut Tx) {
        let rate = self.live.as_ref().map_or(48_000, |l| l.vad.rate());
        let seconds = samples.len() as f64 / rate as f64;
        let wav = audio::wav_bytes(&samples, rate);
        let item = Item { name: "live-turn.wav".into(), kind: "audio".into(), voice: true, seconds: Some(seconds), image: None, source: Source::Bytes(Arc::new(wav)) };
        let mut items = std::mem::take(&mut self.staged);
        items.push(item);
        let (tools, web) = (self.tools, self.web);
        self.send(String::new(), items, "voice".into(), tools, web, true, tx);
    }

    // ---- the frame loop ----------------------------------------------------------------

    /// Apply what arrived and step the clocks. True when the view must re-run.
    pub fn poll(&mut self, now: f64, tx: &mut Tx) -> bool {
        self.now = now;
        let mut changed = false;
        while let Ok(ev) = self.events.try_recv() {
            self.apply(ev, tx);
            changed = true;
        }
        // Playback ending.
        if let Some((_, session)) = self.playing {
            if !self.speaker.is_playing(session) {
                self.playing = None;
                changed = true;
                if self.live.as_ref().is_some_and(|l| l.phase == LivePhase::Speaking) {
                    let saved = self.live.as_ref().is_some_and(|l| l.saved);
                    if let Some(live) = &mut self.live {
                        if saved {
                            live.phase = LivePhase::Saved;
                            live.next = Some((now + 1.1, LivePhase::Listening));
                        } else {
                            live.next = Some((now + 0.25, LivePhase::Listening));
                        }
                    }
                }
            }
        }
        // The microphone (or `--hear`'s recording, for tests).
        if self.recording.is_some() {
            if let Some((samples, rate)) = self.hear.take() {
                self.mic.inject(&samples, rate);
            }
        }
        if let Some(rec) = &mut self.recording {
            let samples = self.mic.take();
            if !samples.is_empty() {
                let rms = (samples.iter().map(|s| s * s).sum::<f32>() / samples.len() as f32).sqrt();
                rec.level = rec.level * 0.6 + (rms * 6.0).min(1.0) * 0.4;
                rec.samples.extend(samples);
            }
            changed = true;
        }
        if self.live.is_some() {
            changed = true;
            self.step_live(now, tx);
        }
        let animating = changed && (self.live.is_some() || self.recording.is_some()) || self.turn.is_some() || self.playing.is_some();
        if animating {
            tx.wake_after(std::time::Duration::from_millis(16));
            changed = true;
        }
        // The offline card waits out a short grace period (a restart shouldn't flash it): draw
        // again when that ends, as nothing else may.
        if let Server::Down { since, .. } = &self.server {
            let wait = OFFLINE_GRACE + 0.05 - (now - since);
            if wait > 0.0 {
                tx.wake_after(std::time::Duration::from_secs_f64(wait));
            }
            if wait > -0.5 {
                changed = true;
            }
        }
        if self.launched.is_some() {
            let before = (self.launching_at.is_some(), self.launch_line.clone());
            self.watch_launch(now);
            changed |= before != (self.launching_at.is_some(), self.launch_line.clone());
        }
        if self.launching_at.is_some() && !matches!(self.server, Server::Up(_)) {
            tx.wake_after(std::time::Duration::from_millis(500));
            changed = true;
        }
        changed
    }

    fn step_live(&mut self, now: f64, tx: &mut Tx) {
        if self.live.as_ref().is_some_and(|l| l.phase == LivePhase::Listening && !l.muted) {
            if let Some((mut samples, rate)) = self.hear.take() {
                samples.extend(std::iter::repeat_n(0.0, rate as usize * 3 / 2));
                self.mic.inject(&samples, rate);
            }
        }
        let samples = self.mic.take();
        let turn_live = self.turn.is_some();
        let Some(live) = &mut self.live else { return };
        if let Some((at, phase)) = live.next.clone() {
            if now >= at {
                live.next = None;
                if !(turn_live && phase == LivePhase::Listening) {
                    live.phase = phase;
                    if live.phase == LivePhase::Listening {
                        live.saved = false;
                    }
                }
            }
        }
        let mode = if live.muted {
            Mode::Muted
        } else {
            match live.phase {
                LivePhase::Listening | LivePhase::Hearing => Mode::Listen,
                LivePhase::Thinking | LivePhase::Searching | LivePhase::Reading | LivePhase::Speaking => Mode::Busy { barge: live.barge },
                _ => Mode::Busy { barge: false },
            }
        };
        let heard = live.vad.feed(&samples, mode);
        let rms = live.vad.level;
        live.level = live.level * 0.6 + (rms * 8.0).min(1.0) * 0.4;
        for h in heard {
            match h {
                Heard::Began => {
                    if let Some(live) = &mut self.live {
                        live.phase = LivePhase::Hearing;
                        live.next = None;
                    }
                }
                Heard::Barge => {
                    self.stop_reply();
                    if let Some(live) = &mut self.live {
                        live.phase = LivePhase::Hearing;
                        live.next = None;
                    }
                }
                Heard::TooShort => {
                    if let Some(live) = &mut self.live {
                        live.phase = LivePhase::Listening;
                    }
                }
                Heard::Utterance(samples) => self.live_utterance(samples, tx),
            }
        }
        // A talk-over waiting for the cancelled turn.
        if self.turn.is_none() {
            if let Some(samples) = self.live.as_mut().and_then(|l| l.pending.take()) {
                self.send_live(samples, tx);
            }
        }
    }

    pub fn apply(&mut self, ev: Ev, tx: &mut Tx) {
        match ev {
            Ev::Status(Ok(status)) => {
                let gone = !matches!(status.state.as_str(), "ready" | "starting");
                if gone {
                    if let Some(live) = &mut self.live {
                        live.phase = LivePhase::Error("The model isn't running.".into());
                    }
                } else if let Some(live) = &mut self.live {
                    if matches!(&live.phase, LivePhase::Error(e) if e.contains("model")) && status.ready() {
                        live.phase = LivePhase::Listening;
                    }
                }
                self.server = Server::Up(status);
                self.launching_at = None;
            }
            Ev::Status(Err(error)) => {
                if !matches!(self.server, Server::Down { .. }) {
                    self.server = Server::Down { since: self.now, error };
                }
            }
            Ev::LabFetched(result) => {
                self.fetching_lab = false;
                match result {
                    Ok(dir) => self.choose_lab(dir),
                    Err(e) => self.lab_problem = Some(e),
                }
            }
            Ev::ModelAction(result) => {
                self.model_action = None;
                match result {
                    Ok(status) => self.server = Server::Up(status),
                    Err(e) => self.note(e),
                }
            }
            Ev::Picture { msg, step, bytes } => {
                let Some(cache) = self.images.clone() else { return };
                if let Some(b) = self.bot_mut(msg) {
                    let mut n = 0;
                    for seg in &mut b.segs {
                        if let Seg::Tool(t) = seg {
                            if n == step {
                                t.picture = Some(cache.load(bytes));
                                break;
                            }
                            n += 1;
                        }
                    }
                }
            }
            Ev::Replay { msg, clip } => {
                self.loading_replay = None;
                match clip {
                    Ok((samples, rate)) => {
                        let session = self.speaker.play_clip(&samples, rate);
                        self.playing = Some((msg, session));
                    }
                    Err(e) => self.note(e),
                }
            }
            Ev::Sample { name, bytes, starter } => match bytes {
                Ok(bytes) => {
                    let kind = api::kind_of(&name).to_string();
                    let bytes = Arc::new(bytes);
                    let image = if kind == "image" { self.images.as_ref().map(|c| c.load(bytes.as_slice().to_vec())) } else { None };
                    let voice = false;
                    let item = Item { name, kind, voice, seconds: None, image, source: Source::Bytes(bytes) };
                    self.send_starter(starter, vec![item], tx);
                }
                Err(e) => self.note(e),
            },
            Ev::Turn(id, ev) => self.apply_turn(id, ev, tx),
        }
    }

    fn apply_turn(&mut self, id: u64, ev: TurnEv, tx: &mut Tx) {
        let Some(turn) = self.turn.as_ref().filter(|t| t.id == id) else { return };
        let (user, bot, live_turn) = (turn.user, turn.bot, turn.live);
        match ev {
            TurnEv::Uploaded(media) => {
                if let Some(t) = &mut self.turn {
                    t.retry.media = Some(media.iter().map(|m| m.id.clone()).collect());
                }
                if let Some(u) = self.user_mut(user) {
                    for (item, m) in u.items.iter_mut().zip(&media) {
                        item.kind = m.kind.clone();
                        if m.seconds.is_some() {
                            item.seconds = m.seconds;
                        }
                    }
                }
            }
            TurnEv::Started { rid, speak } => {
                let stop = self.turn.as_ref().is_some_and(|t| t.stop.load(Ordering::Relaxed));
                if let Some(t) = &mut self.turn {
                    t.rid = Some(rid.clone());
                }
                if stop {
                    let api = self.api.clone();
                    std::thread::spawn(move || api.cancel(&rid));
                }
                if let Some(b) = self.bot_mut(bot) {
                    b.state = BotState::Streaming;
                    b.speak = speak;
                }
                self.live_phase(LivePhase::Thinking);
            }
            TurnEv::Transcript(text) => {
                if let Some(u) = self.user_mut(user) {
                    u.transcript = Some(text.clone());
                }
                if live_turn {
                    if let Some(live) = &mut self.live {
                        live.heard = text;
                    }
                }
            }
            TurnEv::Token(text) => {
                if let Some(b) = self.bot_mut(bot) {
                    match b.segs.last_mut() {
                        Some(Seg::Text(t)) => t.push_str(&text),
                        _ => b.segs.push(Seg::Text(text.clone())),
                    }
                }
                if live_turn {
                    if let Some(live) = &mut self.live {
                        live.caption.push_str(&text);
                    }
                }
            }
            TurnEv::Audio(session) => {
                if let Some(t) = &mut self.turn {
                    t.session = Some(session);
                }
                self.playing = Some((bot, session));
                if live_turn {
                    let now = self.now;
                    if let Some(live) = &mut self.live {
                        live.answered_in = live.speech_end.map(|e| now - e);
                    }
                    self.live_phase(LivePhase::Speaking);
                }
            }
            TurnEv::ToolCall { name, args } => {
                let phase = match name.as_str() {
                    "web_search" => LivePhase::Searching,
                    "open_page" | "click" | "type_text" | "go_back" => LivePhase::Reading,
                    _ => LivePhase::Thinking,
                };
                if let Some(b) = self.bot_mut(bot) {
                    b.segs.push(Seg::Tool(ToolStep { name, args, result: None, picture: None }));
                }
                self.live_phase(phase);
            }
            TurnEv::ToolResult { name, result, screenshot } => {
                let saved = name == "add_reminder" && result.get("saved").and_then(Value::as_bool).unwrap_or(false);
                let mut step = None;
                if let Some(b) = self.bot_mut(bot) {
                    let tools = b.segs.iter().filter(|s| matches!(s, Seg::Tool(_))).count();
                    if let Some(Seg::Tool(t)) = b.segs.iter_mut().rev().find(|s| matches!(s, Seg::Tool(t) if t.name == name && t.result.is_none())) {
                        t.result = Some(result);
                        step = Some(tools - 1);
                    }
                }
                if let (Some(path), Some(step)) = (screenshot, step) {
                    let (api, sender, waker) = (self.api.clone(), self.sender.clone(), self.waker.clone());
                    std::thread::spawn(move || {
                        if let Ok(bytes) = api.get(&path) {
                            let _ = sender.send(Ev::Picture { msg: bot, step, bytes });
                            waker();
                        }
                    });
                }
                if saved {
                    if let Some(live) = &mut self.live {
                        live.saved = true;
                    }
                }
                self.live_phase(LivePhase::Thinking);
            }
            TurnEv::Trimmed(left_out) => {
                let pos = self.messages.iter().position(|m| m.id == bot).unwrap_or(self.messages.len());
                let id = self.id();
                self.messages.insert(pos, Msg { id, version: 0, body: Body::Note(format!("Left out the {left_out} earliest messages to fit the model's 32K context.")) });
            }
            TurnEv::Done { text, audio, stats, append } => {
                self.history.extend(append);
                let cancelled = stats.get("finish").and_then(Value::as_str) == Some("cancelled");
                let session = self.turn.as_ref().and_then(|t| t.session);
                if let Some(b) = self.bot_mut(bot) {
                    match b.segs.last_mut() {
                        Some(Seg::Text(t)) => *t = text,
                        _ if !text.is_empty() => b.segs.push(Seg::Text(text)),
                        _ => {}
                    }
                    b.state = if cancelled { BotState::Cancelled } else { BotState::Done };
                    b.stats = Some(stats.clone());
                    b.audio = audio;
                }
                self.turn = None;
                if live_turn {
                    let now = self.now;
                    if let Some(live) = &mut self.live {
                        live.stats = Some(stats);
                        if session.is_none() && live.pending.is_none() && !matches!(live.phase, LivePhase::Hearing) {
                            // Nothing was spoken: listen again.
                            live.next = Some((now + 0.25, LivePhase::Listening));
                        }
                    }
                }
                tx.scroll_to_bottom("thread");
            }
            TurnEv::Failed(error) => {
                let retry = self.turn.as_ref().map(|t| t.retry.clone());
                self.speaker.stop();
                self.playing = None;
                if let Some(b) = self.bot_mut(bot) {
                    b.state = BotState::Failed(error.clone());
                    b.retry = retry;
                }
                self.turn = None;
                if live_turn {
                    let now = self.now;
                    if let Some(live) = &mut self.live {
                        if live.phase != LivePhase::Hearing {
                            live.phase = LivePhase::Error(error);
                            live.next = Some((now + 2.5, LivePhase::Listening));
                        }
                    }
                }
            }
        }
    }

    fn live_phase(&mut self, phase: LivePhase) {
        if !self.turn.as_ref().is_some_and(|t| t.live) {
            return;
        }
        if let Some(live) = &mut self.live {
            if matches!(live.phase, LivePhase::Hearing) {
                return;
            }
            live.phase = phase;
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn run_turn(id: u64, api: Api, speaker: Speaker, history: Vec<Value>, retry: Retry, stop: Arc<AtomicBool>, sender: &Sender<Ev>, waker: &Waker) {
    let send = |ev: TurnEv| {
        let _ = sender.send(Ev::Turn(id, ev));
        waker();
    };
    // Uploads first, in attachment order (the model matches media by order).
    let media = match retry.media.clone() {
        Some(ids) => ids,
        None => {
            let mut uploaded = Vec::new();
            for item in &retry.items {
                let bytes = match &item.source {
                    Source::File(path) => match std::fs::read(path) {
                        Ok(b) => b,
                        Err(e) => return send(TurnEv::Failed(format!("Couldn't read {}: {e}", item.name))),
                    },
                    Source::Bytes(b) => b.as_ref().clone(),
                };
                match api.upload(&item.name, &bytes, item.voice) {
                    Ok(m) => uploaded.push(m),
                    Err(e) => return send(TurnEv::Failed(e)),
                }
            }
            let ids = uploaded.iter().map(|m| m.id.clone()).collect();
            send(TurnEv::Uploaded(uploaded));
            ids
        }
    };
    let request = api::chat_request(&history, &retry.text, &media, &retry.reply, retry.tools, retry.web);
    let log = std::env::var("LFM3_NATIVE_LOG").is_ok_and(|v| v == "1");
    let t0 = std::time::Instant::now();
    if log {
        eprintln!("lfm3-omni: turn {id}: chat request sent");
    }
    let mut session: Option<u64> = None;
    let b64 = base64::engine::general_purpose::STANDARD;
    let result = api.chat(&request, |event| {
        let kind = event["event"].as_str().unwrap_or("");
        if log {
            eprintln!("lfm3-omni: turn {id}: {:>7.3}s {kind}", t0.elapsed().as_secs_f64());
        }
        match kind {
            "started" => send(TurnEv::Started { rid: event["id"].as_str().unwrap_or("").into(), speak: event["speak"].as_bool().unwrap_or(false) }),
            "transcript" => send(TurnEv::Transcript(event["text"].as_str().unwrap_or("").into())),
            "token" => send(TurnEv::Token(event["text"].as_str().unwrap_or("").into())),
            "audio" => {
                if stop.load(Ordering::Relaxed) {
                    return;
                }
                let Ok(bytes) = b64.decode(event["pcm"].as_str().unwrap_or("")) else { return };
                let rate = event["rate"].as_u64().unwrap_or(24_000) as u32;
                let s = *session.get_or_insert_with(|| {
                    let s = speaker.begin();
                    send(TurnEv::Audio(s));
                    s
                });
                speaker.push(s, &audio::pcm16_to_f32(&bytes), rate);
            }
            "tool_call" => send(TurnEv::ToolCall { name: event["name"].as_str().unwrap_or("").into(), args: event["arguments"].clone() }),
            "tool_result" => send(TurnEv::ToolResult {
                name: event["name"].as_str().unwrap_or("").into(),
                result: event["result"].clone(),
                screenshot: event["screenshot"].as_str().map(str::to_string),
            }),
            "trimmed" => send(TurnEv::Trimmed(event["left_out"].as_i64().unwrap_or(0))),
            "done" => {
                if let Some(s) = session {
                    speaker.finish(s);
                }
                send(TurnEv::Done {
                    text: event["text"].as_str().unwrap_or("").into(),
                    audio: event["audio"].as_str().map(str::to_string),
                    stats: event["stats"].clone(),
                    append: event["append"].as_array().cloned().unwrap_or_default(),
                });
            }
            "error" => send(TurnEv::Failed(event["message"].as_str().unwrap_or("Something went wrong.").into())),
            _ => {}
        }
    });
    match result {
        Ok(true) => {}
        Ok(false) => send(TurnEv::Failed("The connection dropped before the answer finished.".into())),
        Err(e) => send(TurnEv::Failed(e)),
    }
}
