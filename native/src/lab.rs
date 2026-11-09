//! Where the lab is: the folder whose start script "Start lab server" runs.
//!
//! In order: $LFM3_LAB_DIR; the folder chosen in the app (kept with the prefs); the checkout
//! this was built from, except in a release build (`--features dist`), which is made on
//! another machine; then the usual places for a clone. A downloaded app that finds none of
//! these asks for the folder.

use std::path::{Path, PathBuf};

/// The script that sets the lab up and starts its server.
pub const START: &str = if cfg!(windows) { "start.ps1" } else { "start.sh" };

/// A folder holding the lab: its start script and its server.
pub fn is_lab(dir: &Path) -> bool {
    dir.join(START).is_file() && dir.join("app").join("server.py").is_file()
}

pub fn home() -> Option<PathBuf> {
    std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).map(PathBuf::from)
}

pub fn find() -> Option<PathBuf> {
    if let Some(dir) = std::env::var_os("LFM3_LAB_DIR") {
        return Some(PathBuf::from(dir));
    }
    let mut candidates: Vec<PathBuf> = crate::prefs::lab_dir().into_iter().collect();
    #[cfg(not(feature = "dist"))]
    if let Some(dir) = Path::new(env!("CARGO_MANIFEST_DIR")).parent() {
        candidates.push(dir.to_path_buf());
    }
    if let Some(home) = home() {
        for folder in ["projects", "Projects", "Developer", "code", "src", "GitHub", "Documents", "Desktop", "Downloads", ""] {
            candidates.push(home.join(folder).join("lfm3-omni-lab"));
        }
    }
    candidates.into_iter().find(|dir| is_lab(dir))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_lab_needs_its_start_script_and_server() {
        let dir = std::env::temp_dir().join(format!("lfm3-lab-test-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("app")).unwrap();
        assert!(!is_lab(&dir));
        std::fs::write(dir.join(START), "").unwrap();
        assert!(!is_lab(&dir));
        std::fs::write(dir.join("app").join("server.py"), "").unwrap();
        assert!(is_lab(&dir));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[cfg(not(feature = "dist"))]
    #[test]
    fn a_build_from_a_checkout_finds_it() {
        // SAFETY: tests in this module don't read LFM3_LAB_DIR concurrently.
        if std::env::var_os("LFM3_LAB_DIR").is_none() {
            assert!(find().is_some_and(|dir| is_lab(&dir)));
        }
    }
}
