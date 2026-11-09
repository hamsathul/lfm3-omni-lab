//! Getting the lab running from the app: fetching it onto a computer that has only this app,
//! starting its server, following the server's first-run output, and stopping it.
//!
//! A downloaded app fetches the lab's source for its own version from GitHub into
//! ~/lfm3-omni-lab. The lab's start script then does the rest on first run: it makes the
//! Python environment, installs the packages and downloads the model if it isn't there yet.

use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::process::{Child, Command};

pub const REPO: &str = "https://github.com/hamsathul/lfm3-omni-lab";

/// Where "Set up the lab" puts it.
pub fn default_dir() -> Option<PathBuf> {
    crate::lab::home().map(|home| home.join("lfm3-omni-lab"))
}

/// A folder as people know it: ~/lfm3-omni-lab rather than /Users/name/lfm3-omni-lab.
pub fn shown(dir: &Path) -> String {
    match crate::lab::home() {
        Some(home) if !cfg!(windows) && dir.starts_with(&home) => {
            format!("~/{}", dir.strip_prefix(&home).unwrap_or(dir).display())
        }
        _ => dir.display().to_string(),
    }
}

/// No console window for helper programs on Windows.
pub fn quiet(cmd: &mut Command) -> &mut Command {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    cmd
}

fn run(cmd: &mut Command, what: &str) -> Result<(), String> {
    let out = quiet(cmd).output().map_err(|e| format!("Couldn't {what}: {e}"))?;
    if out.status.success() {
        return Ok(());
    }
    let err = String::from_utf8_lossy(&out.stderr);
    let detail = err.lines().rev().find(|l| !l.trim().is_empty()).unwrap_or("it failed").trim().to_string();
    Err(format!("Couldn't {what} ({detail})."))
}

/// Download the lab into `dest` (which must not exist, or be empty) and return it.
pub fn fetch(dest: &Path) -> Result<PathBuf, String> {
    if crate::lab::is_lab(dest) {
        return Ok(dest.to_path_buf());
    }
    if dest.read_dir().is_ok_and(|mut d| d.next().is_some()) {
        return Err(format!("{} already exists and isn't the lab. Move it away, or choose the lab's folder.", dest.display()));
    }
    let parent = dest.parent().ok_or("There's no folder to put the lab in.")?;
    let work = parent.join(".lfm3-omni-lab-download");
    let _ = std::fs::remove_dir_all(&work);
    std::fs::create_dir_all(&work).map_err(|e| format!("Couldn't make {}: {e}", work.display()))?;
    let result = fetch_into(&work, dest);
    let _ = std::fs::remove_dir_all(&work);
    result
}

fn fetch_into(work: &Path, dest: &Path) -> Result<PathBuf, String> {
    let zip = work.join("lab.zip");
    let curl = if cfg!(windows) { "curl.exe" } else { "/usr/bin/curl" };
    // This app's own version first; the newest code if that version has no tag.
    let tag = format!("{REPO}/archive/refs/tags/v{}.zip", env!("CARGO_PKG_VERSION"));
    let main = format!("{REPO}/archive/refs/heads/main.zip");
    run(Command::new(curl).args(["-fsSL", "--retry", "2", "-o"]).arg(&zip).arg(&tag), "download the lab")
        .or_else(|_| run(Command::new(curl).args(["-fsSL", "--retry", "2", "-o"]).arg(&zip).arg(&main), "download the lab"))?;
    let unpacked = work.join("unpacked");
    std::fs::create_dir_all(&unpacked).map_err(|e| e.to_string())?;
    if cfg!(windows) {
        run(Command::new("tar.exe").arg("-xf").arg(&zip).arg("-C").arg(&unpacked), "unpack the lab")?;
    } else {
        run(Command::new("/usr/bin/ditto").args(["-x", "-k"]).arg(&zip).arg(&unpacked), "unpack the lab")?;
    }
    // The archive holds one folder, lfm3-omni-lab-<version>.
    let inner = std::fs::read_dir(&unpacked)
        .map_err(|e| e.to_string())?
        .filter_map(Result::ok)
        .map(|e| e.path())
        .find(|p| crate::lab::is_lab(p))
        .ok_or("The download didn't hold the lab.")?;
    let _ = std::fs::remove_dir(dest); // an empty folder in the way
    std::fs::rename(&inner, dest).map_err(|e| format!("Couldn't move the lab to {}: {e}", dest.display()))?;
    Ok(dest.to_path_buf())
}

/// The server's first-run output goes here, in the lab's data folder.
pub fn log_path(lab: &Path) -> PathBuf {
    lab.join("data").join("native-server.log")
}

/// Start the lab's start script without opening a browser. Its output goes to the log.
pub fn start(lab: &Path, port: u16) -> std::io::Result<Child> {
    let log = log_path(lab);
    let _ = std::fs::create_dir_all(lab.join("data"));
    let out = std::fs::File::create(&log)?;
    let mut cmd = if cfg!(windows) {
        let mut c = Command::new("powershell.exe");
        c.args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", crate::lab::START]);
        c
    } else {
        // Opened from Finder the app has a bare PATH; the script and the server look for
        // Homebrew's python and ffmpeg.
        let path = std::env::var("PATH").unwrap_or_default();
        let path = format!("/opt/homebrew/bin:/usr/local/bin:{}", if path.is_empty() { "/usr/bin:/bin:/usr/sbin:/sbin" } else { &path });
        let mut c = Command::new("/bin/bash");
        c.arg(crate::lab::START).env("PATH", path);
        c
    };
    cmd.current_dir(lab).env("NO_BROWSER", "1").env("PORT", port.to_string()).stdout(out.try_clone()?).stderr(out);
    quiet(&mut cmd).spawn()
}

/// Stop a server this app started. On a Mac SIGTERM lets it stop the model on its way out;
/// on Windows the whole tree goes (the script, the server, the model and its llama-server).
pub fn stop(child: &mut Child) {
    let pid = child.id().to_string();
    if cfg!(windows) {
        let _ = quiet(Command::new("taskkill").args(["/PID", &pid, "/T", "/F"])).status();
    } else {
        let _ = Command::new("kill").arg("-TERM").arg(&pid).status();
    }
    let _ = child.try_wait();
}

/// The last line the server's start wrote: what it's doing (installing, downloading the model).
pub fn last_line(log: &Path) -> Option<String> {
    let mut file = std::fs::File::open(log).ok()?;
    let len = file.metadata().ok()?.len();
    file.seek(SeekFrom::Start(len.saturating_sub(4096))).ok()?;
    let mut tail = Vec::new();
    file.read_to_end(&mut tail).ok()?;
    let tail = String::from_utf8_lossy(&tail);
    // Progress bars redraw with \r; the newest one is the last piece.
    let line = tail.split(['\r', '\n']).map(strip_ansi).map(|l| l.trim().to_string()).rfind(|l| !l.is_empty())?;
    Some(if line.chars().count() > 160 { line.chars().take(157).collect::<String>() + "…" } else { line })
}

fn strip_ansi(line: &str) -> String {
    let mut out = String::with_capacity(line.len());
    let mut chars = line.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\u{1b}' {
            if chars.peek() == Some(&'[') {
                chars.next();
                for c in chars.by_ref() {
                    if c.is_ascii_alphabetic() {
                        break;
                    }
                }
            }
            continue;
        }
        out.push(c);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_last_line_is_the_newest_progress() {
        let log = std::env::temp_dir().join(format!("lfm3-log-test-{}", std::process::id()));
        std::fs::write(&log, "First run: creating .venv\nDownloading model\r\x1b[32m 10%\x1b[0m |##  |\r 55% |#####|\r\n\n").unwrap();
        assert_eq!(last_line(&log).as_deref(), Some("55% |#####|"));
        std::fs::remove_file(&log).unwrap();
    }

    /// Downloads the lab from GitHub (about 10 MB): `cargo test -- --ignored fetching_downloads`.
    #[test]
    #[ignore]
    fn fetching_downloads_the_lab() {
        let root = std::env::temp_dir().join(format!("lfm3-fetch-real-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        let dest = root.join("lfm3-omni-lab");
        let got = fetch(&dest).unwrap();
        assert!(crate::lab::is_lab(&got));
        assert!(got.join("requirements.txt").is_file());
        assert!(!root.join(".lfm3-omni-lab-download").exists(), "the download folder is cleaned up");
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn fetching_refuses_a_folder_in_the_way() {
        let dir = std::env::temp_dir().join(format!("lfm3-fetch-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("notes.txt"), "mine").unwrap();
        let err = fetch(&dir).unwrap_err();
        assert!(err.contains("already exists"), "{err}");
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
