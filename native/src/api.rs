//! The lab server's HTTP API, as static/app.js uses it: model status and control, media
//! uploads, the NDJSON chat stream, cancel, and files under /outputs and /samples. Blocking
//! calls, made from worker threads.

use serde_json::{Value, json};
use std::io::{BufRead, BufReader};
use std::time::Duration;

#[derive(Clone)]
pub struct Api {
    pub base: String,
    quick: ureq::Agent,
    long: ureq::Agent,
}

/// One uploaded file, as `POST /api/media` answers.
#[derive(Clone, Debug)]
pub struct Media {
    pub id: String,
    pub kind: String,
    pub seconds: Option<f64>,
}

/// The model process, from `GET /api/model`.
#[derive(Clone, Debug, PartialEq)]
pub struct ModelStatus {
    pub state: String,
    pub busy: bool,
    pub model: String,
    pub starting_seconds: Option<i64>,
    pub memory_gb: Option<f64>,
    pub error: Option<String>,
}

impl ModelStatus {
    pub fn from_json(v: &Value) -> ModelStatus {
        ModelStatus {
            state: v["state"].as_str().unwrap_or("error").to_string(),
            busy: v["busy"].as_bool().unwrap_or(false),
            model: v["model"].as_str().unwrap_or("").to_string(),
            starting_seconds: v["starting_seconds"].as_i64(),
            // Rounded, so a status poll that only moves the last digit doesn't redraw.
            memory_gb: v["memory_gb"].as_f64().map(|g| (g * 10.0).round() / 10.0),
            error: v["error"].as_str().map(str::to_string),
        }
    }
    pub fn ready(&self) -> bool {
        self.state == "ready"
    }
}

fn error_of(status: u16, body: &str) -> String {
    serde_json::from_str::<Value>(body)
        .ok()
        .and_then(|v| v["error"].as_str().map(str::to_string))
        .unwrap_or_else(|| format!("The server answered {status}."))
}

fn unreachable(e: impl std::fmt::Display) -> String {
    // ureq says "io: Connection refused (os error 61)": keep the human part.
    let e = e.to_string();
    let e = e.strip_prefix("io: ").unwrap_or(&e);
    let e = match e.rfind(" (os error ") {
        Some(i) if e.ends_with(')') => &e[..i],
        _ => e,
    };
    format!("Can't reach the lab server ({e}).")
}

impl Api {
    pub fn new(base: &str) -> Api {
        let quick = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(4)))
            .http_status_as_error(false)
            .build()
            .into();
        let long = ureq::Agent::config_builder()
            .timeout_connect(Some(Duration::from_secs(4)))
            .http_status_as_error(false)
            .build()
            .into();
        Api { base: base.trim_end_matches('/').to_string(), quick, long }
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base, path)
    }

    pub fn status(&self) -> Result<ModelStatus, String> {
        let mut r = self.quick.get(&self.url("/api/model")).call().map_err(unreachable)?;
        let body = r.body_mut().read_to_string().map_err(unreachable)?;
        let v: Value = serde_json::from_str(&body).map_err(|e| e.to_string())?;
        Ok(ModelStatus::from_json(&v))
    }

    /// `start`, `stop` or `restart`.
    pub fn model(&self, action: &str) -> Result<ModelStatus, String> {
        let mut r = self.long.post(&self.url(&format!("/api/model/{action}"))).send_empty().map_err(unreachable)?;
        let status = r.status().as_u16();
        let body = r.body_mut().read_to_string().map_err(unreachable)?;
        if status != 200 {
            return Err(error_of(status, &body));
        }
        let v: Value = serde_json::from_str(&body).map_err(|e| e.to_string())?;
        Ok(ModelStatus::from_json(&v))
    }

    /// Upload a file. `voice` marks a voice message (a spoken reply, a transcript first).
    pub fn upload(&self, name: &str, bytes: &[u8], voice: bool) -> Result<Media, String> {
        let boundary = format!("lfm3native{:x}", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_nanos()));
        let safe_name = name.replace(['"', '\r', '\n'], "_");
        let mut body = Vec::with_capacity(bytes.len() + 512);
        body.extend_from_slice(format!("--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{safe_name}\"\r\nContent-Type: {}\r\n\r\n", mime_of(name)).as_bytes());
        body.extend_from_slice(bytes);
        body.extend_from_slice(b"\r\n");
        if voice {
            body.extend_from_slice(format!("--{boundary}\r\nContent-Disposition: form-data; name=\"voice\"\r\n\r\n1\r\n").as_bytes());
        }
        body.extend_from_slice(format!("--{boundary}--\r\n").as_bytes());
        let mut r = self
            .long
            .post(&self.url("/api/media"))
            .header("Content-Type", &format!("multipart/form-data; boundary={boundary}"))
            .send(&body[..])
            .map_err(unreachable)?;
        let status = r.status().as_u16();
        let text = r.body_mut().read_to_string().map_err(unreachable)?;
        if status != 200 {
            return Err(error_of(status, &text));
        }
        let v: Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
        Ok(Media {
            id: v["id"].as_str().unwrap_or_default().to_string(),
            kind: v["kind"].as_str().unwrap_or("audio").to_string(),
            seconds: v["seconds"].as_f64(),
        })
    }

    /// `POST /api/chat`, calling `on_event` for each NDJSON event as it arrives. Ok(true) when
    /// the stream ended with `done` or `error`, Ok(false) when it dropped before either.
    pub fn chat(&self, request: &Value, mut on_event: impl FnMut(Value)) -> Result<bool, String> {
        let body = serde_json::to_vec(request).map_err(|e| e.to_string())?;
        let mut r = self
            .long
            .post(&self.url("/api/chat"))
            .header("Content-Type", "application/json")
            .send(&body[..])
            .map_err(unreachable)?;
        let status = r.status().as_u16();
        if status != 200 {
            let text = r.body_mut().read_to_string().unwrap_or_default();
            return Err(error_of(status, &text));
        }
        let mut reader = BufReader::new(r.into_body().into_reader());
        let mut line = Vec::new();
        let mut terminal = false;
        loop {
            line.clear();
            // Split on newline bytes before decoding, so a UTF-8 character is never cut.
            match reader.read_until(b'\n', &mut line) {
                Ok(0) => break,
                Ok(_) => {}
                Err(_) => break,
            }
            let text = String::from_utf8_lossy(&line);
            let text = text.trim();
            if text.is_empty() {
                continue;
            }
            let Ok(event) = serde_json::from_str::<Value>(text) else { continue };
            if matches!(event["event"].as_str(), Some("done" | "error")) {
                terminal = true;
            }
            on_event(event);
        }
        Ok(terminal)
    }

    pub fn cancel(&self, rid: &str) {
        let _ = self.quick.post(&self.url(&format!("/api/cancel/{rid}"))).send_empty();
    }

    /// A file the server serves (`/outputs/…`, `/samples/…`).
    pub fn get(&self, path: &str) -> Result<Vec<u8>, String> {
        let mut r = self.long.get(&self.url(path)).call().map_err(unreachable)?;
        let status = r.status().as_u16();
        if status != 200 {
            return Err(format!("The server answered {status} for {path}."));
        }
        r.body_mut().with_config().limit(200 * 1024 * 1024).read_to_vec().map_err(|e| e.to_string())
    }
}

pub fn mime_of(name: &str) -> &'static str {
    let ext = name.rsplit('.').next().unwrap_or("").to_ascii_lowercase();
    match ext.as_str() {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "heic" | "heif" => "image/heic",
        "wav" => "audio/wav",
        "mp3" => "audio/mpeg",
        "m4a" => "audio/mp4",
        "aac" => "audio/aac",
        "ogg" | "oga" | "opus" => "audio/ogg",
        "flac" => "audio/flac",
        "aiff" | "aif" => "audio/aiff",
        "caf" => "audio/x-caf",
        "mp4" | "m4v" => "video/mp4",
        "mov" => "video/quicktime",
        "webm" => "video/webm",
        "mkv" => "video/x-matroska",
        "avi" => "video/x-msvideo",
        _ => "application/octet-stream",
    }
}

/// What kind an attachment is, from its name, before the server says.
pub fn kind_of(name: &str) -> &'static str {
    let m = mime_of(name);
    if m.starts_with("image/") {
        "image"
    } else if m.starts_with("video/") {
        "video"
    } else {
        "audio"
    }
}

/// The chat request app.js sends: the whole history, the new message and the turn's options.
pub fn chat_request(history: &[Value], text: &str, media: &[String], reply: &str, tools: bool, web: bool) -> Value {
    json!({
        "history": history,
        "message": { "text": text, "media": media },
        "reply": reply,
        "tools": tools,
        "web": web,
        "system": "",
        "temperature": 0,
        "max_tokens": 1024,
    })
}

#[cfg(test)]
mod tests {
    #[test]
    fn connection_errors_read_plainly() {
        assert_eq!(super::unreachable("io: Connection refused (os error 61)"), "Can't reach the lab server (Connection refused).");
        assert_eq!(super::unreachable("timeout: global"), "Can't reach the lab server (timeout: global).");
    }
}
