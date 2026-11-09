//! Live mode fills the window: your words large at the top, the orb in the middle, the reply
//! as captions under it, and an edge glow in the state's colour (the page's "A" design).

use crate::look::{self, Glyph, Pal, glyph};
use crate::model::{LivePhase, State};
use crate::orb;
use rune::{Color, Cursor, Cx, El, Shadow, col, el, row, text};

fn tail(s: &str, max: usize) -> String {
    let s = s.trim();
    if s.chars().count() <= max {
        return s.to_string();
    }
    let skip = s.chars().count() - max;
    let rest: String = s.chars().skip(skip).collect();
    // Start at a word.
    match rest.find(' ') {
        Some(i) if i < 24 => format!("…{}", &rest[i + 1..]),
        _ => format!("…{rest}"),
    }
}

pub fn view(s: &State, cx: &mut Cx<State>, p: &Pal) -> El {
    let Some(live) = &s.live else { return el() };
    let inset = cx.titlebar_inset();
    let mood = live.mood();
    let color = mood.color(p.dark);
    let t = s.now as f32;
    let level = match live.phase {
        LivePhase::Speaking => s.speaker.level(),
        LivePhase::Hearing | LivePhase::Listening => live.level,
        _ => 0.0,
    };
    let glow_strength = match live.phase {
        LivePhase::Listening if live.muted => 0.12,
        LivePhase::Listening => 0.28,
        LivePhase::Hearing => 0.45 + 0.4 * live.level,
        LivePhase::Speaking => 0.45 + 0.4 * level,
        LivePhase::Error(_) => 0.55,
        _ => 0.42 + 0.1 * (t * 3.0).sin(),
    };

    let top = row().key("live-top").w_full().h(52.0).pl(inset.max(8.0) + 10.0).pr(14.0).gap(10.0).center_y().window_drag()
        .child(text("Live").font(look::TITLE).color(p.text))
        .child(text(if s.web && s.tools { "Web on" } else if s.tools { "Tools on" } else { "Tools off" }).font(look::SMALL_BOLD).color(p.text3))
        .child(el().grow(1.0))
        .child(
            el().key("live-close").size(30.0, 30.0).center().radius(15.0).fill(p.control).hover(p.control_hover).cursor(Cursor::Pointer)
                .help("Back to chat (Esc)")
                .child(glyph(Glyph::Close, 14.0, p.text2))
                .on_press(|s: &mut State| s.exit_live()),
        );

    let words = if live.heard.is_empty() {
        match live.phase {
            LivePhase::Hearing => "…".to_string(),
            LivePhase::Listening if !live.muted => "Go ahead, I'm listening".to_string(),
            _ => String::new(),
        }
    } else {
        tail(&live.heard, 140)
    };
    let words_color = if live.heard.is_empty() { p.text3 } else { p.text };
    let heard = el().key("heard").w_full().max_w(720.0).min_h(80.0).px(32.0).center()
        .child(text(words).w_full().font(look::LIVE_WORDS).color(words_color).wrap().align(rune::Align::Center).max_lines(3).motion(rune::tok::motion::DEFAULT));

    let orb_box = col().key("live-orb").grow(1.0).w_full().min_h(0.0).center()
        .child(orb::orb(300.0, t, mood, level, p.dark));

    let mut status = row().gap(8.0).center_y().child(el().size(8.0, 8.0).radius(4.0).fill(color).motion(rune::tok::motion::DEFAULT))
        .child(text(live.label()).font(look::LABEL).color(p.text2));
    if let Some(a) = live.answered_in {
        status = status.child(text(format!("· answered in {a:.1} s")).font(look::SMALL).color(p.text3));
    }
    if let Some(st) = &live.stats {
        if let Some(tps) = st["tokens_per_sec"].as_f64() {
            status = status.child(text(format!("· {tps:.0} tok/s")).font(look::SMALL).color(p.text3));
        }
    }
    let caption = el().key("caption").w_full().max_w(640.0).min_h(76.0).px(32.0).center()
        .child(text(tail(&live.caption, 220)).w_full().font(look::LIVE_CAPTION).color(p.text2).wrap().align(rune::Align::Center).max_lines(3));

    let busy = matches!(live.phase, LivePhase::Sending | LivePhase::Thinking | LivePhase::Searching | LivePhase::Reading | LivePhase::Speaking);
    let mut controls = row().key("live-controls").gap(12.0).center_y()
        .child(
            control("live-mute", if live.muted { Glyph::MicOff } else { Glyph::Mic }, if live.muted { "Unmute" } else { "Mute" }, live.muted, p)
                .on_press(|s: &mut State| {
                    if let Some(l) = &mut s.live {
                        l.muted = !l.muted;
                        l.vad.reset();
                        if l.muted && l.phase == LivePhase::Hearing {
                            l.phase = LivePhase::Listening;
                        }
                    }
                }),
        );
    if busy {
        controls = controls.child(control("live-interrupt", Glyph::Stop, "Interrupt", false, p).on_press(|s: &mut State| s.interrupt()));
    }
    controls = controls.child(
        el().key("live-barge").h(36.0).px(14.0).center().radius(18.0)
            .fill(if live.barge { p.mine.alpha(0.16) } else { p.control })
            .border(1.0, if live.barge { p.mine.alpha(0.6) } else { Color::TRANSPARENT })
            .cursor(Cursor::Pointer)
            .role("switch").label("Let me talk over it").checked(live.barge)
            .help("Cut in by talking while it answers. Works best with headphones: without echo cancellation, the speakers can trigger it.")
            .child(text("Talk over").font(look::LABEL).color(if live.barge { p.mine } else { p.text2 }))
            .on_press(|s: &mut State| {
                if let Some(l) = &mut s.live {
                    l.barge = !l.barge;
                    crate::prefs::save_barge(l.barge);
                }
            }),
    );
    controls = controls.child(
        el().key("live-end").h(36.0).px(18.0).center().radius(18.0).fill(p.red).cursor(Cursor::Pointer)
            .child(text("End").font(look::BODY_BOLD).color(Color::WHITE))
            .on_press(|s: &mut State| s.exit_live()),
    );

    col().key("live").w_full().h_full().fill(p.live_bg)
        .box_shadow(Shadow { x: 0.0, y: 0.0, blur: 90.0, spread: 6.0, color: color.alpha(glow_strength), inset: true })
        .child(top)
        .child(el().h(10.0))
        .child(row().w_full().justify_center().child(heard))
        .child(orb_box)
        .child(row().w_full().justify_center().child(status))
        .child(el().h(8.0))
        .child(row().w_full().justify_center().child(caption))
        .child(row().w_full().justify_center().pt(12.0).pb(26.0).child(controls))
        .on_drop_files(|s: &mut State, files, _| s.attach_paths(files))
}

fn control(key: &str, g: Glyph, label: &str, on: bool, p: &Pal) -> El {
    row().key(key.to_string()).h(36.0).pl(12.0).pr(14.0).gap(6.0).center_y().radius(18.0)
        .fill(if on { p.text } else { p.control })
        .hover(if on { p.text } else { p.control_hover })
        .cursor(Cursor::Pointer)
        .child(glyph(g, 16.0, if on { p.bg } else { p.text2 }))
        .child(text(label.to_string()).font(look::LABEL).color(if on { p.bg } else { p.text2 }))
}
