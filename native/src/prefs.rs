//! The few choices worth keeping between launches (the page keeps them in localStorage):
//! reply mode, Tools, Web, live mode's talk-over, and the lab folder when it was chosen here.
//! They live in ~/Library/Application Support/LFM3-Omni Native on a Mac and in
//! %APPDATA%\LFM3-Omni Native on Windows.

use crate::model::Reply;
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

pub struct Prefs {
    pub reply: Reply,
    pub tools: bool,
    pub web: bool,
}

pub struct Loaded {
    pub reply: Reply,
    pub tools: bool,
    pub web: bool,
    pub barge: bool,
}

fn path() -> Option<PathBuf> {
    if std::env::var_os("LFM3_NATIVE_NO_PREFS").is_some() {
        return None;
    }
    let folder = if cfg!(windows) {
        PathBuf::from(std::env::var_os("APPDATA")?)
    } else {
        PathBuf::from(std::env::var_os("HOME")?).join("Library/Application Support")
    };
    Some(folder.join("LFM3-Omni Native").join("prefs.json"))
}

fn read() -> Value {
    path().and_then(|p| std::fs::read(p).ok()).and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or(Value::Null)
}

fn write(v: &Value) {
    let Some(p) = path() else { return };
    if let Some(dir) = p.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::write(p, serde_json::to_vec_pretty(v).unwrap_or_default());
}

pub fn load() -> Loaded {
    let v = read();
    let reply = match v["reply"].as_str() {
        Some("text") => Reply::Text,
        Some("voice") => Reply::Voice,
        _ => Reply::Auto,
    };
    Loaded { reply, tools: v["tools"].as_bool().unwrap_or(true), web: v["web"].as_bool().unwrap_or(false), barge: v["barge"].as_bool().unwrap_or(false) }
}

pub fn save(p: &Prefs) {
    let mut v = read();
    if !v.is_object() {
        v = json!({});
    }
    v["reply"] = p.reply.wire().into();
    v["tools"] = p.tools.into();
    v["web"] = p.web.into();
    write(&v);
}

pub fn save_barge(on: bool) {
    let mut v = read();
    if !v.is_object() {
        v = json!({});
    }
    v["barge"] = on.into();
    write(&v);
}

/// The lab folder chosen or set up in the app, if any.
pub fn lab_dir() -> Option<PathBuf> {
    read()["lab_dir"].as_str().map(PathBuf::from)
}

pub fn save_lab_dir(dir: &Path) {
    let mut v = read();
    if !v.is_object() {
        v = json!({});
    }
    v["lab_dir"] = dir.to_string_lossy().into_owned().into();
    write(&v);
}
