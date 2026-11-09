//! Named states for headless snapshots (`--snap out.png --scene chat`), so each screen can be
//! checked without a server.

use crate::api::ModelStatus;
use crate::model::*;
use serde_json::json;
use std::sync::Arc;

pub const SCENES: &[(&str, fn(&mut State))] = &[
    ("offline", offline),
    ("no-lab", no_lab),
    ("launching", launching),
    ("stopped", stopped),
    ("empty", ready),
    ("chat", chat),
    ("streaming", streaming),
    ("failed", failed),
    ("recording", recording),
    ("live", live_listening),
    ("live-hearing", live_hearing),
    ("live-speaking", live_speaking),
    ("live-searching", live_searching),
    ("icon", icon),
];

fn icon(s: &mut State) {
    s.icon = true;
    s.now = 1.1;
}

fn status(state: &str) -> ModelStatus {
    ModelStatus {
        state: state.into(),
        busy: false,
        model: "LiquidAI/LFM3-Omni-2B-MLX-8bit".into(),
        starting_seconds: None,
        memory_gb: if state == "ready" { Some(3.1) } else { None },
        error: None,
    }
}

fn no_lab(s: &mut State) {
    offline(s);
    s.lab_dir = None;
}

fn launching(s: &mut State) {
    offline(s);
    s.launching_at = Some(0.0);
    s.launch_line = Some("model-q8.safetensors:  41%|████      | 1.19G/2.90G [00:38<00:52, 32.6MB/s]".into());
}

fn offline(s: &mut State) {
    s.server = Server::Down { since: -10.0, error: "Can't reach the lab server (Connection refused).".into() };
}

fn stopped(s: &mut State) {
    s.server = Server::Up(status("stopped"));
}

fn ready(s: &mut State) {
    s.server = Server::Up(status("ready"));
}

fn sample_image(s: &mut State) -> Option<rune::Image> {
    let cache = s.images.get_or_insert_with(|| Arc::new(rune::ImageCache::new(Arc::new(|| {})))).clone();
    let path = s.lab_dir.clone().unwrap_or_default().join("static/samples/sign.png");
    let img = cache.file(path, 240.0, 2.0);
    let _ = img.wait();
    Some(img)
}

fn push(s: &mut State, body: Body) -> u64 {
    let id = s.messages.len() as u64 + 100;
    s.messages.push(Msg { id, version: 0, body });
    id
}

fn bot(segs: Vec<Seg>, state: BotState, stats: Option<serde_json::Value>, audio: Option<&str>) -> Body {
    Body::Bot(BotMsg { segs, state, stats, audio: audio.map(str::to_string), speak: audio.is_some(), retry: None })
}

fn chat(s: &mut State) {
    ready(s);
    let img = sample_image(s);
    push(s, Body::User(UserMsg {
        text: "What does this sign say?".into(),
        items: vec![Item { name: "sign.png".into(), kind: "image".into(), voice: false, seconds: None, image: img, source: Source::Bytes(Arc::new(Vec::new())) }],
        transcript: None,
        live: false,
    }));
    push(s, bot(
        vec![Seg::Text("The sign says “STOP — Private road, no through traffic.” It's a red octagonal stop sign with a white rectangular plate underneath.".into())],
        BotState::Done,
        Some(json!({"seconds": 2.4, "tokens_per_sec": 41.2, "prompt_tokens": 1830, "context_tokens": 32768, "tool_rounds": 0})),
        None,
    ));
    push(s, Body::User(UserMsg {
        text: String::new(),
        items: vec![Item { name: "voice-message.wav".into(), kind: "audio".into(), voice: true, seconds: Some(3.4), image: None, source: Source::Bytes(Arc::new(Vec::new())) }],
        transcript: Some("What time is it in Tokyo right now, and what's 17.5% of 2,340?".into()),
        live: false,
    }));
    push(s, bot(
        vec![
            Seg::Tool(ToolStep { name: "get_current_time".into(), args: json!({"timezone": "Asia/Tokyo"}), result: Some(json!({"time": "21:14", "date": "2026-11-09"})), picture: None }),
            Seg::Tool(ToolStep { name: "calculate".into(), args: json!({"expression": "0.175 * 2340"}), result: Some(json!({"expression": "0.175 * 2340", "result": 409.5})), picture: None }),
            Seg::Text("It's 9:14 in the evening in Tokyo. And 17.5% of 2,340 is 409.5.".into()),
        ],
        BotState::Done,
        Some(json!({"seconds": 5.8, "tokens_per_sec": 36.0, "audio_seconds": 4.6, "prompt_tokens": 2410, "context_tokens": 32768, "tool_rounds": 2})),
        Some("abc.wav"),
    ));
    push(s, Body::User(UserMsg { text: "Look up the latest Python release.".into(), items: vec![], transcript: None, live: false }));
    let shot = sample_image(s);
    push(s, bot(
        vec![
            Seg::Tool(ToolStep { name: "web_search".into(), args: json!({"query": "latest Python release"}), result: Some(json!({"results": [1, 2, 3, 4, 5]})), picture: None }),
            Seg::Tool(ToolStep { name: "open_page".into(), args: json!({"n": 1}), result: Some(json!({"title": "Python Release Python 3.15.0 | Python.org"})), picture: shot }),
            Seg::Text("The latest Python release is **Python 3.15.0**, released in October 2026.".into()),
        ],
        BotState::Done,
        Some(json!({"seconds": 11.2, "tokens_per_sec": 39.4, "prompt_tokens": 5120, "context_tokens": 32768, "tool_rounds": 2})),
        None,
    ));
    s.now = 3.0;
}

fn streaming(s: &mut State) {
    ready(s);
    let user = push(s, Body::User(UserMsg { text: "Tell me a short story about a lighthouse keeper.".into(), items: vec![], transcript: None, live: false }));
    let b = push(s, bot(vec![Seg::Text("Every night for forty years, Mara climbed the hundred and twelve steps of the lighthouse".into())], BotState::Streaming, None, None));
    s.turn = Some(Turn {
        id: 1,
        user,
        bot: b,
        rid: Some("abc".into()),
        stop: Default::default(),
        session: None,
        live: false,
        retry: Retry { text: String::new(), items: vec![], media: None, reply: "text".into(), tools: true, web: false },
    });
    s.draft = rune::TextEdit::new("");
    s.now = 1.2;
}

fn failed(s: &mut State) {
    ready(s);
    push(s, Body::User(UserMsg { text: "Describe what happens in this clip.".into(), items: vec![], transcript: None, live: false }));
    let retry = Retry { text: "x".into(), items: vec![], media: None, reply: "auto".into(), tools: true, web: false };
    push(s, Body::Bot(BotMsg { segs: vec![], state: BotState::Failed("The connection dropped before the answer finished.".into()), stats: None, audio: None, speak: false, retry: Some(retry) }));
    push(s, Body::Note("Left out the 4 earliest messages to fit the model's 32K context.".into()));
}

fn recording(s: &mut State) {
    chat(s);
    s.recording = Some(Recording { started: 0.0, samples: vec![], rate: 48_000, level: 0.55 });
    s.now = 4.3;
}

fn live_base(s: &mut State, phase: LivePhase) {
    ready(s);
    s.live = Some(Live {
        phase,
        vad: crate::vad::Vad::new(48_000),
        heard: String::new(),
        caption: String::new(),
        muted: false,
        barge: false,
        level: 0.0,
        next: None,
        speech_end: None,
        answered_in: None,
        stats: None,
        pending: None,
        saved: false,
    });
    s.now = 2.0;
}

fn live_listening(s: &mut State) {
    live_base(s, LivePhase::Listening);
}

fn live_hearing(s: &mut State) {
    live_base(s, LivePhase::Hearing);
    if let Some(l) = &mut s.live {
        l.level = 0.7;
    }
}

fn live_speaking(s: &mut State) {
    live_base(s, LivePhase::Speaking);
    if let Some(l) = &mut s.live {
        l.heard = "Tell me something surprising about octopuses.".into();
        l.caption = "Octopuses have three hearts and blue blood. Two hearts pump blood to the gills, and the third pumps it to the rest of the body.".into();
        l.answered_in = Some(1.4);
        l.stats = Some(json!({"tokens_per_sec": 38.2}));
        l.barge = true;
    }
}

fn live_searching(s: &mut State) {
    live_base(s, LivePhase::Searching);
    s.web = true;
    if let Some(l) = &mut s.live {
        l.heard = "What's the weather like in Dubai this weekend?".into();
    }
}
