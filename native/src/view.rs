//! The chat window: a top bar with the model's status and the turn options, the thread, and a
//! pill message bar whose orb opens live mode.

use crate::look::{self, Glyph, Pal, glyph};
use crate::model::{BotMsg, BotState, Body, Item, Msg, Reply, STARTERS, Seg, Server, State, ToolStep, UserMsg};
use crate::orb::{self, Mood};
use rune::{Color, Cursor, Cx, El, ScrollSpec, Shadow, SurfaceFit, Transition, Tx, col, el, memo, row, text, text_input};
use serde_json::Value;

const THREAD_W: f32 = 720.0;

pub fn view(s: &State, cx: &mut Cx<State>) -> El {
    let dark = cx.system_dark();
    let p = look::pal(dark);
    if s.icon {
        return app_icon();
    }
    if s.live.is_some() {
        return crate::live_view::view(s, cx, &p);
    }
    let inset = cx.titlebar_inset();
    let compact = cx.layout_with("root", |r| r[2] < 820.0).unwrap_or(false);
    let body = match &s.server {
        Server::Down { since, error } if s.now - since > crate::model::OFFLINE_GRACE || s.launching_at.is_some() => server_down(s, &p, error),
        Server::Unknown | Server::Down { .. } => col().grow(1.0).w_full(),
        Server::Up(_) => thread(s, &p),
    };
    let mut root = col()
        .key("root")
        .w_full()
        .h_full()
        .fill(p.bg)
        .child(top_bar(s, &p, inset, compact))
        .child(body)
        .on_drop_files(|s: &mut State, files, _| {
            s.drop_hover = false;
            s.attach_paths(files);
        })
        .on_drop_hover(|s: &mut State, over, _| s.drop_hover = over);
    if matches!(s.server, Server::Up(_)) {
        root = root.child(composer(s, &p));
    }
    if s.drop_hover {
        root = root.child(
            el().absolute(0.0, 0.0)
                .w_full()
                .h_full()
                .center()
                .fill(Color { a: 0.12, ..p.mine })
                .border(2.0, p.mine)
                .child(text("Drop to attach").font(look::TITLE).color(p.mine))
                .transition(Transition::opacity()),
        );
    }
    root
}

/// The app icon (the `icon` scene, rendered at 512 pt × 2): the orb on a dark rounded square,
/// inside the margins macOS icons keep.
fn app_icon() -> El {
    let tile = el().size(412.0, 412.0).radius(92.0).center()
        .linear_gradient(std::f32::consts::PI, vec![(0.0, look::rgb(40, 44, 62)), (1.0, look::rgb(10, 11, 18))])
        .box_shadow(Shadow { x: 0.0, y: 10.0, blur: 24.0, spread: 0.0, color: Color { a: 0.35, ..Color::BLACK }, inset: false })
        .child(orb::orb(330.0, 1.1, Mood::Thinking, 0.0, true));
    el().size(512.0, 512.0).center().child(tile)
}

// ---- top bar --------------------------------------------------------------------------

fn status_label(s: &State, compact: bool) -> (String, Color) {
    let p = look::pal(false);
    match &s.server {
        Server::Unknown => ("Connecting".into(), p.text3),
        Server::Down { .. } => ("Server offline".into(), p.red),
        Server::Up(st) => match st.state.as_str() {
            "ready" if st.busy => ("Working".into(), p.mine),
            "ready" if compact => ("Ready".into(), p.green),
            "ready" => (st.memory_gb.map_or("Ready".into(), |g| format!("Ready · {g:.1} GB")), p.green),
            "starting" => (format!("Starting{}", st.starting_seconds.map_or(String::new(), |t| format!(" · {t} s"))), p.amber),
            "stopping" => ("Stopping".into(), p.amber),
            "stopped" => ("Model stopped".into(), p.text3),
            _ => ("Model error".into(), p.red),
        },
    }
}

fn top_bar(s: &State, p: &Pal, inset: f32, compact: bool) -> El {
    let (label, dot) = status_label(s, compact);
    let mut bar = row()
        .key("topbar")
        .w_full()
        .h(52.0)
        .pl(inset.max(8.0) + 10.0)
        .pr(12.0)
        .gap(if compact { 8.0 } else { 10.0 })
        .center_y()
        .fill(p.bar)
        .window_drag();
    if !compact {
        bar = bar.child(text("LFM3-Omni").font(look::TITLE).color(p.text));
    }
    bar = bar.child(
            row().key("status").h(24.0).px(9.0).gap(6.0).center_y().radius(12.0).fill(p.control)
                .help(s.status().map_or("The lab server".into(), |st| st.model.clone()))
                .child(el().size(7.0, 7.0).radius(3.5).fill(dot).motion(rune::tok::motion::DEFAULT))
                .child(text(label).font(look::SMALL_BOLD).color(p.text2)),
    );
    if let Some(st) = s.status() {
        let busy = s.model_action.is_some();
        let (action, label) = match (st.state.as_str(), compact) {
            ("ready" | "starting", false) => ("stop", "Stop model"),
            ("ready" | "starting", true) => ("stop", "Stop"),
            ("error", false) => ("restart", "Restart model"),
            ("error", true) => ("restart", "Restart"),
            (_, false) => ("start", "Start model"),
            (_, true) => ("start", "Start"),
        };
        if st.state != "stopping" {
            bar = bar.child(chip(label, false, p, busy).key("model-action").on_press(move |s: &mut State| s.model_action(action)));
        }
    }
    bar = bar.child(el().grow(1.0));
    if matches!(s.server, Server::Up(_)) {
        let mut seg = row().key("reply").h(28.0).padding(2.0).radius(8.0).fill(p.control).gap(2.0).help("How the model answers");
        for (r, name) in [(Reply::Auto, "Auto"), (Reply::Text, "Text"), (Reply::Voice, "Voice")] {
            let on = s.reply == r;
            seg = seg.child(
                el().key(format!("reply-{name}")).h(24.0).px(10.0).center().radius(6.0)
                    .fill(if on { p.bar } else { Color::TRANSPARENT })
                    .shadow(0.0, if on { 2.0 } else { 0.0 }, Color { a: if on { 0.12 } else { 0.0 }, ..Color::BLACK })
                    .cursor(Cursor::Pointer)
                    .child(text(name).font(look::SMALL_BOLD).color(if on { p.text } else { p.text2 }))
                    .on_press(move |s: &mut State| {
                        s.reply = r;
                        s.save_prefs();
                    }),
            );
        }
        bar = bar
            .child(seg)
            .child(toggle("Tools", s.tools, p, "Clock, calculator and reminders").key("tools").on_press(|s: &mut State| {
                s.tools = !s.tools;
                s.save_prefs();
            }))
            .child(toggle("Web", s.web && s.tools, p, "Search and read web pages (needs Tools)").key("web").on_press(|s: &mut State| {
                s.web = !s.web;
                if s.web {
                    s.tools = true;
                }
                s.save_prefs();
            }))
            .child(
                el().key("new-chat").size(30.0, 30.0).center().radius(8.0).hover(p.control_hover).cursor(Cursor::Pointer)
                    .help(if cfg!(target_os = "macos") { "New chat (⌘N)" } else { "New chat (Ctrl+N)" })
                    .child(glyph(Glyph::Compose, 18.0, p.text2))
                    .on_press(|s: &mut State| s.new_chat()),
            );
    }
    col().w_full().child(bar).child(el().w_full().h(1.0).fill(p.line))
}

fn chip(label: &str, on: bool, p: &Pal, dim: bool) -> El {
    el().h(26.0).px(11.0).center().radius(13.0)
        .fill(if on { p.mine } else { p.control })
        .hover(if on { p.mine } else { p.control_hover })
        .cursor(Cursor::Pointer)
        .opacity(if dim { 0.5 } else { 1.0 })
        .child(text(label.to_string()).font(look::SMALL_BOLD).color(if on { Color::WHITE } else { p.text }))
}

fn toggle(label: &str, on: bool, p: &Pal, help: &str) -> El {
    chip(label, on, p, false).help(help.to_string()).role("switch").label(label.to_string()).checked(on)
}

// ---- server offline -------------------------------------------------------------------

fn server_down(s: &State, p: &Pal, error: &str) -> El {
    let launching = s.launching_at.is_some();
    let fetching = s.fetching_lab;
    let busy = launching || fetching;
    let (title, body) = if fetching {
        ("Downloading the lab…".to_string(), format!("Getting LFM3-Omni Lab from {} into your home folder.", crate::setup::REPO.trim_start_matches("https://")))
    } else if launching {
        (
            "Starting the lab server…".to_string(),
            "The first start installs its Python packages and downloads the model (about 3 GB), which takes a while. \
             Later starts take seconds."
                .to_string(),
        )
    } else if s.lab_dir.is_none() {
        (
            "Set up LFM3-Omni Lab".to_string(),
            format!(
                "This window talks to LFM3-Omni Lab, which runs the model on this computer. Set it up here: it downloads \
                 the lab into {}, installs what it needs and the model (about 3 GB), and starts it. Or choose a copy you already have.",
                crate::setup::default_dir().map_or("your home folder".into(), |d| crate::setup::shown(&d))
            ),
        )
    } else {
        (
            "The lab server isn't running".to_string(),
            format!("This window talks to LFM3-Omni Lab at {}. Start it here, or run {} yourself.", s.api.base, crate::lab::START),
        )
    };
    let mut card = col().max_w(460.0).gap(14.0).items_center()
        .child(orb::orb(120.0, s.now as f32, if busy { Mood::Thinking } else { Mood::Muted }, 0.0, p.dark))
        .child(text(title).font(look::HEADLINE).color(p.text))
        .child(text(body).font(look::BODY).color(p.text2).wrap().align(rune::Align::Center));
    if launching {
        if let Some(line) = &s.launch_line {
            card = card.child(text(line.clone()).font(look::SMALL).color(p.text3).wrap().align(rune::Align::Center));
        }
    }
    let button = |key: &'static str, label: &str, primary: bool| {
        el().key(key).h(36.0).px(18.0).center().radius(18.0).cursor(Cursor::Pointer)
            .fill(if primary { p.mine } else { p.control })
            .child(text(label.to_string()).font(look::BODY_BOLD).color(if primary { Color::WHITE } else { p.text }))
    };
    let choose = |label: &str, primary: bool| {
        button("choose-lab", label, primary).on_press_tx(|_: &mut State, tx: &mut Tx| {
            let options = rune::FileDialogOptions { title: "Choose the LFM3-Omni Lab folder".into(), ..Default::default() };
            tx.open_folder(options, |s: &mut State, result, _| {
                if let Ok(Some(dir)) = result {
                    s.choose_lab(dir);
                }
            });
        })
    };
    if !busy {
        let buttons = if s.lab_dir.is_none() {
            row().gap(10.0)
                .child(button("fetch-lab", "Set up the lab", true).on_press(|s: &mut State| s.fetch_lab()))
                .child(choose("Choose lab folder…", false))
        } else {
            row().gap(10.0).child(button("launch", "Start lab server", true).on_press(|s: &mut State| s.launch_server()))
        };
        card = card.child(buttons);
        let detail = s.lab_problem.clone().unwrap_or_else(|| error.to_string());
        card = card.child(text(detail).font(look::SMALL).color(if s.lab_problem.is_some() { p.text2 } else { p.text3 }).wrap().align(rune::Align::Center));
    }
    col().grow(1.0).w_full().center().padding(24.0).child(card)
}

// ---- thread ---------------------------------------------------------------------------

fn thread(s: &State, p: &Pal) -> El {
    if s.messages.is_empty() {
        return empty_state(s, p);
    }
    let mut list = col().key("rows").w_full().max_w(THREAD_W).gap(14.0).pt(18.0).pb(10.0).px(20.0);
    let active = s.turn.as_ref().map(|t| t.bot);
    let n = s.messages.len();
    for (i, m) in s.messages.iter().enumerate() {
        let live_row = active == Some(m.id) || s.playing.is_some_and(|(id, _)| id == m.id) || s.loading_replay == Some(m.id) || i + 1 == n;
        let ready = images_ready(m);
        let row_el = if live_row {
            message(s, m, p)
        } else {
            memo(format!("m{}", m.id), (m.version, p.dark, ready), || message(s, m, p))
        };
        list = list.child(row_el.key(format!("msg-{}", m.id)).transition(Transition::offset(0.0, 8.0).combined(Transition::opacity())));
    }
    col().key("thread").w_full().grow(1.0).min_h(0.0).items_center()
        .scroll(ScrollSpec { stick_bottom: true })
        .scroll_edge_fade(14.0)
        .child(list)
}

fn images_ready(m: &Msg) -> usize {
    match &m.body {
        Body::User(u) => u.items.iter().filter(|i| i.image.as_ref().is_some_and(|im| im.is_ready())).count(),
        Body::Bot(b) => b.segs.iter().filter(|s| matches!(s, Seg::Tool(t) if t.picture.as_ref().is_some_and(|im| im.is_ready()))).count(),
        Body::Note(_) => 0,
    }
}

fn empty_state(s: &State, p: &Pal) -> El {
    let ready = s.ready();
    let mut starters = row().gap(8.0).flex_wrap().justify_center().max_w(560.0);
    for (i, st) in STARTERS.iter().enumerate() {
        starters = starters.child(
            el().key(format!("starter-{i}")).h(32.0).px(14.0).center().radius(16.0).fill(p.bar).border(1.0, p.line)
                .hover(p.control_hover)
                .cursor(Cursor::Pointer)
                .opacity(if ready { 1.0 } else { 0.45 })
                .help(st.text.to_string())
                .child(text(st.label).font(look::LABEL).color(p.text))
                .on_press_tx(move |s: &mut State, tx: &mut Tx| s.starter(i, tx)),
        );
    }
    let (headline, sub) = match s.status().map(|st| st.state.as_str()) {
        Some("ready") => ("What can I help with?", "Type, drop in a photo, audio or video, record a voice message, or tap the orb to talk."),
        Some("starting") => ("Loading the model…", "LFM3-Omni-2B is loading into memory. This takes a few seconds."),
        _ => ("The model is stopped", runs_here(s)),
    };
    let mood = match s.status().map(|st| st.state.as_str()) {
        Some("ready") => Mood::Listening,
        Some("starting") => Mood::Thinking,
        _ => Mood::Muted,
    };
    let mut c = col().key("empty").grow(1.0).w_full().center().gap(14.0).padding(24.0)
        .child(orb::orb(150.0, s.now as f32, mood, 0.0, p.dark))
        .child(text(headline).font(look::HEADLINE).color(p.text))
        .child(text(sub).font(look::BODY).color(p.text2).wrap().max_w(460.0).align(rune::Align::Center));
    if ready {
        c = c.child(el().h(6.0)).child(starters);
    } else if s.status().is_some_and(|st| matches!(st.state.as_str(), "stopped" | "error")) {
        c = c.child(
            el().key("start-model").h(36.0).px(18.0).center().radius(18.0).fill(p.mine).cursor(Cursor::Pointer)
                .opacity(if s.model_action.is_some() { 0.5 } else { 1.0 })
                .child(text("Start model").font(look::BODY_BOLD).color(Color::WHITE))
                .on_press(|s: &mut State| s.model_action("start")),
        );
        if let Some(e) = s.status().and_then(|st| st.error.clone()) {
            c = c.child(text(e).font(look::SMALL).color(p.red).wrap().max_w(460.0));
        }
    }
    c
}

fn message(s: &State, m: &Msg, p: &Pal) -> El {
    match &m.body {
        Body::User(u) => user_msg(u, p),
        Body::Bot(b) => bot_msg(s, m.id, b, p),
        Body::Note(n) => row().w_full().justify_center().child(text(n.clone()).font(look::SMALL).color(p.text2).wrap().align(rune::Align::Center)),
    }
}

fn user_msg(u: &UserMsg, p: &Pal) -> El {
    let mut c = col().w_full().items_end().gap(6.0);
    if !u.items.is_empty() {
        let mut cards = row().gap(6.0).flex_wrap().justify_end().max_w(520.0);
        for item in &u.items {
            cards = cards.child(item_card(item, p));
        }
        c = c.child(cards);
    }
    if !u.text.is_empty() {
        c = c.child(
            col().max_w(520.0).px(14.0).py(9.0).radius(18.0).fill(p.mine)
                .child(text(u.text.clone()).font(look::BODY).color(Color::WHITE).wrap().selectable(true)),
        );
    }
    if let Some(t) = &u.transcript {
        c = c.child(text(format!("“{}”", t.trim())).font(look::SMALL.italic()).color(p.text2).wrap().max_w(480.0).align(rune::Align::End).selectable(true));
    }
    if u.live {
        c = c.child(text("Live").font(look::SMALL_BOLD).color(p.text3));
    }
    c
}

fn item_card(item: &Item, p: &Pal) -> El {
    if let Some(img) = &item.image {
        let (w, h) = match img.size() {
            Some([iw, ih]) if iw > 0 && ih > 0 => {
                let scale = (220.0 / iw as f32).min(160.0 / ih as f32);
                ((iw as f32 * scale).max(80.0), (ih as f32 * scale).max(60.0))
            }
            _ => (180.0, 120.0),
        };
        return el().size(w, h).radius(14.0).clip_rounded(14.0).fill(p.glass)
            .help(item.name.clone())
            .child(el().w_full().h_full().pixels_fit(img.pixels(), SurfaceFit::Fill));
    }
    let (icon, label) = if item.voice {
        (Glyph::Mic, match item.seconds {
            Some(t) => format!("Voice message · {t:.1} s"),
            None => "Voice message".into(),
        })
    } else if item.kind == "video" {
        (Glyph::Film, item.name.clone())
    } else if item.kind == "image" {
        (Glyph::Photo, item.name.clone())
    } else {
        (Glyph::Wave, match item.seconds {
            Some(t) => format!("{} · {t:.1} s", item.name),
            None => item.name.clone(),
        })
    };
    row().h(40.0).px(12.0).gap(8.0).center_y().radius(14.0).fill(p.glass)
        .child(glyph(icon, 16.0, p.on_glass))
        .child(text(label).font(look::LABEL).color(p.on_glass).truncate().max_w(240.0))
}

fn stats_line(stats: &Value) -> String {
    let mut parts = Vec::new();
    if let Some(sec) = stats["seconds"].as_f64() {
        parts.push(format!("{sec:.1} s"));
    }
    if let Some(tps) = stats["tokens_per_sec"].as_f64() {
        parts.push(format!("{tps:.0} tok/s"));
    }
    if let Some(a) = stats["audio_seconds"].as_f64() {
        if a > 0.0 {
            parts.push(format!("{a:.1} s of speech"));
        }
    }
    if let Some(r) = stats["tool_rounds"].as_i64() {
        if r > 0 {
            parts.push(format!("{r} tool round{}", if r == 1 { "" } else { "s" }));
        }
    }
    if let (Some(pt), Some(ctx)) = (stats["prompt_tokens"].as_f64(), stats["context_tokens"].as_f64().or(Some(32768.0))) {
        parts.push(format!("context {:.0}%", 100.0 * pt / ctx.max(1.0)));
    }
    parts.join(" · ")
}

fn bot_msg(s: &State, id: u64, b: &BotMsg, p: &Pal) -> El {
    let mut c = col().w_full().items_start().gap(6.0);
    let mut tool_i = 0;
    for seg in &b.segs {
        match seg {
            Seg::Text(t) if !t.trim().is_empty() => {
                c = c.child(
                    col().max_w(560.0).px(14.0).py(9.0).radius(18.0).fill(p.bubble)
                        .child(crate::markdown::rich_text(t.trim(), p).wrap().selectable(true)),
                );
            }
            Seg::Text(_) => {}
            Seg::Tool(t) => {
                c = c.child(tool_step(t, p));
                tool_i += 1;
            }
        }
    }
    let _ = tool_i;
    let speaking = s.playing.is_some_and(|(m, _)| m == id);
    match &b.state {
        BotState::Waiting | BotState::Streaming => {
            let mood = if speaking { Mood::Speaking } else { Mood::Thinking };
            let label = if speaking {
                "Speaking"
            } else if b.state == BotState::Waiting {
                "Sending"
            } else if b.segs.last().is_some_and(|s| matches!(s, Seg::Tool(t) if t.result.is_none())) {
                "Using a tool"
            } else if b.segs.last().is_some_and(|s| matches!(s, Seg::Text(t) if !t.trim().is_empty())) {
                "Writing"
            } else if b.speak {
                "Preparing to speak"
            } else {
                "Thinking"
            };
            c = c.child(
                row().gap(8.0).center_y().pl(4.0)
                    .child(orb::orb(22.0, s.now as f32, mood, s.speaker.level(), p.dark))
                    .child(text(label).font(look::SMALL_BOLD).color(p.text2))
                    .child(
                        el().key("stop-inline").h(22.0).px(9.0).center().radius(11.0).fill(p.control).hover(p.control_hover).cursor(Cursor::Pointer)
                            .child(text("Stop").font(look::SMALL_BOLD).color(p.text2))
                            .on_press(|s: &mut State| s.stop_reply()),
                    ),
            );
        }
        BotState::Failed(e) => {
            let mut r = row().gap(8.0).center_y().pl(4.0).child(text(e.clone()).font(look::SMALL).color(p.red).wrap().max_w(460.0));
            if b.retry.is_some() {
                r = r.child(
                    el().key(format!("retry-{id}")).h(24.0).px(10.0).center().radius(12.0).fill(p.control).hover(p.control_hover).cursor(Cursor::Pointer)
                        .child(text("Try again").font(look::SMALL_BOLD).color(p.text))
                        .on_press_tx(move |s: &mut State, tx: &mut Tx| s.retry(id, tx)),
                );
            }
            c = c.child(r);
        }
        BotState::Done | BotState::Cancelled => {
            let mut foot = row().gap(8.0).center_y().pl(4.0);
            if let Some(file) = &b.audio {
                let _ = file;
                let loading = s.loading_replay == Some(id);
                foot = foot.child(
                    row().key(format!("play-{id}")).h(24.0).pl(7.0).pr(10.0).gap(4.0).center_y().radius(12.0).fill(p.control).hover(p.control_hover)
                        .cursor(Cursor::Pointer)
                        .help(if speaking { "Stop" } else { "Play the spoken reply" })
                        .child(if speaking { orb::orb(14.0, s.now as f32, Mood::Speaking, s.speaker.level(), p.dark) } else { glyph(Glyph::Play, 14.0, p.text2) })
                        .child(text(if speaking { "Stop" } else if loading { "Loading" } else { "Play" }).font(look::SMALL_BOLD).color(p.text2))
                        .on_press(move |s: &mut State| s.replay(id)),
                );
            }
            if b.state == BotState::Cancelled {
                foot = foot.child(text("Stopped").font(look::SMALL_BOLD).color(p.text3));
            }
            if let Some(stats) = &b.stats {
                foot = foot.child(text(stats_line(stats)).font(look::SMALL).color(p.text3));
            }
            c = c.child(foot);
        }
    }
    c
}

fn tool_summary(t: &ToolStep) -> (String, String) {
    let a = &t.args;
    let what = match t.name.as_str() {
        "web_search" => format!("Searched “{}”", a["query"].as_str().unwrap_or("")),
        "open_page" => match a["url"].as_str() {
            Some(u) => format!("Opened {u}"),
            None => format!("Opened result {}", a["n"].as_i64().or(a["number"].as_i64()).map_or(String::new(), |n| n.to_string())),
        },
        "click" => match t.result.as_ref().and_then(|r| r["clicked"].as_str()) {
            Some(what) => format!("Clicked the {what}"),
            None => format!("Clicked {}", a["number"].as_i64().map_or(String::new(), |n| n.to_string())),
        },
        "type_text" => format!("Typed “{}”", a["text"].as_str().unwrap_or("")),
        "go_back" => "Went back".to_string(),
        "calculate" => format!("Calculated {}", a["expression"].as_str().unwrap_or("")),
        "get_current_time" => format!("Checked the time{}", a["timezone"].as_str().map_or(String::new(), |z| format!(" in {z}"))),
        "add_reminder" => format!("Reminder: {}", a["text"].as_str().unwrap_or("")),
        "list_reminders" => "Listed reminders".into(),
        other => other.replace('_', " "),
    };
    let r = t.result.as_ref();
    let result = match r {
        None => "…".into(),
        Some(r) if r.get("error").is_some() => format!("Failed: {}", r["error"].as_str().unwrap_or("error")),
        Some(r) => match t.name.as_str() {
            "web_search" => format!("{} results", r["results"].as_array().map_or(0, Vec::len)),
            "open_page" | "click" | "type_text" | "go_back" => r["title"].as_str().unwrap_or("Page read").to_string(),
            "calculate" => format!("= {}", r["result"]),
            "get_current_time" => format!("{} {}", r["time"].as_str().unwrap_or(""), r["date"].as_str().unwrap_or("")),
            "add_reminder" => if r["saved"].as_bool().unwrap_or(false) { "Saved".into() } else { "Not saved".into() },
            "list_reminders" => format!("{} saved", r["count"].as_i64().unwrap_or(0)),
            _ => "Done".into(),
        },
    };
    (what, result)
}

fn tool_step(t: &ToolStep, p: &Pal) -> El {
    let web = crate::model::WEB_TOOLS.contains(&t.name.as_str());
    let color = if web { p.amber } else { p.mine };
    let (what, result) = tool_summary(t);
    let mut c = col().gap(6.0).child(
        row().h(28.0).px(10.0).gap(7.0).center_y().radius(14.0).border(1.0, p.line)
            .child(el().size(6.0, 6.0).radius(3.0).fill(color))
            .child(text(what).font(look::SMALL_BOLD).color(p.text).truncate().max_w(380.0))
            .child(text(result).font(look::SMALL).color(p.text2).truncate().max_w(220.0)),
    );
    if let Some(img) = &t.picture {
        let (w, h) = match img.size() {
            Some([iw, ih]) if iw > 0 => (360.0, (360.0 * ih as f32 / iw as f32).min(240.0)),
            _ => (360.0, 220.0),
        };
        c = c.child(
            el().size(w, h).radius(14.0).clip_rounded(14.0).fill(p.glass)
                .box_shadow(Shadow { x: 0.0, y: 6.0, blur: 18.0, spread: 0.0, color: Color { a: 0.18, ..Color::BLACK }, inset: false })
                .child(el().w_full().h_full().pixels_fit(img.pixels(), SurfaceFit::Fill)),
        );
    }
    c
}

// ---- message bar ------------------------------------------------------------------------

fn composer(s: &State, p: &Pal) -> El {
    let ready = s.ready();
    let busy = s.turn.is_some();
    let mut wrap = col().w_full().items_center().px(20.0).pt(6.0).pb(16.0).gap(8.0);
    if !s.staged.is_empty() {
        let mut chips = row().w_full().max_w(THREAD_W).gap(6.0).flex_wrap();
        for (i, item) in s.staged.iter().enumerate() {
            chips = chips.child(
                row().key(format!("staged-{i}")).gap(4.0).center_y()
                    .child(item_card(item, p).opacity(0.95))
                    .child(
                        el().size(22.0, 22.0).center().radius(11.0).fill(p.control).hover(p.control_hover).cursor(Cursor::Pointer)
                            .help("Remove")
                            .child(glyph(Glyph::Close, 12.0, p.text2))
                            .on_press(move |s: &mut State| {
                                if i < s.staged.len() {
                                    s.staged.remove(i);
                                }
                            }),
                    ),
            );
        }
        wrap = wrap.child(chips);
    }
    let pill = row().key("composer").w_full().max_w(THREAD_W).min_h(48.0).pl(6.0).pr(6.0).py(6.0).gap(6.0).items_end()
        .radius(24.0).fill(p.bar).border(1.0, p.line)
        .box_shadow(Shadow { x: 0.0, y: 4.0, blur: 16.0, spread: 0.0, color: Color { a: if p.dark { 0.35 } else { 0.07 }, ..Color::BLACK }, inset: false });
    let pill = if let Some(rec) = &s.recording {
        recording_bar(pill, s, rec, p)
    } else {
        let attach = round_button("attach", Glyph::Plus, p.control, p.text2, p).help("Attach a photo, recording or video")
            .on_press_tx(|_: &mut State, tx: &mut Tx| {
                let options = rune::FileDialogOptions { title: "Attach".into(), ..Default::default() }
                    .filter("Photos, audio and video", &["png", "jpg", "jpeg", "heic", "webp", "gif", "wav", "mp3", "m4a", "aac", "flac", "ogg", "opus", "aiff", "mp4", "mov", "m4v", "webm", "mkv"]);
                tx.open_file(options, |s: &mut State, result, _| {
                    if let Ok(Some(path)) = result {
                        s.attach_paths(&[path]);
                    }
                });
            });
        let field = text_input(&s.draft, look::BODY)
            .key("draft")
            .grow(1.0)
            .min_h(36.0)
            .py(8.0)
            .px(4.0)
            .color(p.text)
            .placeholder(if ready { "Message LFM3-Omni" } else { "Start the model to chat" }, p.text3)
            .on_edit(|s: &mut State| &mut s.draft)
            .on_submit(|s: &mut State, tx: &mut Tx| s.send_draft(tx));
        let has_input = !s.draft.text.trim().is_empty() || !s.staged.is_empty();
        let mic = round_button("record", Glyph::Mic, p.control, p.text2, p).help("Record a voice message")
            .opacity(if ready && !busy { 1.0 } else { 0.4 })
            .on_press(|s: &mut State| {
                if s.can_send() {
                    s.start_recording()
                }
            });
        let send = if busy {
            round_button("stop", Glyph::Stop, p.text, p.bg, p).help("Stop (Esc)").on_press(|s: &mut State| s.stop_reply())
        } else {
            round_button("send", Glyph::Up, if has_input && ready { p.mine } else { p.control }, if has_input && ready { Color::WHITE } else { p.text3 }, p)
                .help("Send (Return)")
                .on_press_tx(|s: &mut State, tx: &mut Tx| s.send_draft(tx))
        };
        let live = el().key("live").size(36.0, 36.0).center().radius(18.0).hover(p.control_hover).cursor(Cursor::Pointer)
            .help("Live voice mode")
            .opacity(if ready { 1.0 } else { 0.5 })
            .child(orb::orb(32.0, if busy { s.now as f32 } else { 0.8 }, if busy { Mood::Thinking } else { Mood::Listening }, 0.0, p.dark))
            .on_press(|s: &mut State| s.enter_live());
        pill.child(attach).child(field).child(mic).child(send).child(live)
    };
    wrap.child(pill)
}

fn round_button(key: &str, g: Glyph, fill: Color, ink: Color, p: &Pal) -> El {
    let _ = p;
    el().key(key.to_string()).size(36.0, 36.0).center().radius(18.0).fill(fill).cursor(Cursor::Pointer)
        .press_scale(0.92)
        .child(glyph(g, 18.0, ink))
}

fn recording_bar(pill: El, s: &State, rec: &crate::model::Recording, p: &Pal) -> El {
    let secs = s.now - rec.started;
    let mut bars = row().h(36.0).gap(3.0).center_y().grow(1.0).px(6.0);
    for i in 0..28 {
        let phase = (s.now as f32 * 7.0 + i as f32 * 0.7).sin() * 0.5 + 0.5;
        let h = 4.0 + 22.0 * rec.level * (0.35 + 0.65 * phase);
        bars = bars.child(el().w(3.0).h(h).radius(1.5).fill(p.red.alpha(0.8)));
    }
    let blink = (s.now * 2.0).fract() < 0.6;
    pill.child(round_button("rec-cancel", Glyph::Close, p.control, p.text2, p).help("Discard").on_press(|s: &mut State| s.cancel_recording()))
        .child(
            row().h(36.0).gap(8.0).center_y().px(4.0)
                .child(el().size(9.0, 9.0).radius(4.5).fill(p.red).opacity(if blink { 1.0 } else { 0.3 }))
                .child(text(format!("{}:{:02}", secs as u64 / 60, secs as u64 % 60)).font(look::LABEL.mono()).color(p.text)),
        )
        .child(bars)
        .child(round_button("rec-send", Glyph::Up, p.mine, Color::WHITE, p).help("Send voice message").on_press_tx(|s: &mut State, tx: &mut Tx| s.finish_recording(tx)))
}

/// Where the model runs, for the stopped screen.
fn runs_here(s: &State) -> &'static str {
    let llama = matches!(&s.server, Server::Up(status) if status.backend != "mlx");
    match (cfg!(target_os = "macos"), llama) {
        (true, false) => "Start it to chat. It runs on this Mac with MLX; nothing leaves the machine.",
        (true, true) => "Start it to chat. It runs on this Mac with llama.cpp; nothing leaves the machine.",
        (false, _) => "Start it to chat. It runs on this PC with llama.cpp; nothing leaves the machine.",
    }
}
