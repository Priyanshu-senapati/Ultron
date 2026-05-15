//! Configuration loading and on-disk layout.
//!
//! Default location: `%APPDATA%\ULTRON\config.toml`
//! Data dir:         `%APPDATA%\ULTRON\data\`
//! Logs dir:         `%APPDATA%\ULTRON\logs\`
//!
//! On first run, a config with sane defaults and a freshly generated bridge
//! token is written. Edit it to taste; restarts pick up the new values.

use crate::error::{CoreError, CoreResult};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    pub general: GeneralConfig,
    pub bridge: BridgeConfig,
    pub tension: TensionConfig,
    pub input: InputConfig,
    /// New in Phase 1. `#[serde(default)]` so existing Phase 0 `config.toml`
    /// files keep working without a manual migration step.
    #[serde(default)]
    pub perception: PerceptionConfig,
    /// Phase 1, Module O. `#[serde(default)]` so older configs upgrade
    /// transparently.
    #[serde(default)]
    pub insight: InsightConfig,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GeneralConfig {
    pub user_name: String,
    pub data_dir: PathBuf,
    pub logs_dir: PathBuf,
    pub heartbeat_secs: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BridgeConfig {
    pub bind: String,    // "127.0.0.1:9420"
    pub token: String,   // shared secret for ws clients
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TensionConfig {
    pub ewma_alpha: f32,         // 0.0 .. 1.0 — higher = reacts faster
    pub decay_per_sec: f32,      // 0.0 .. 1.0 — natural decline of tension
    pub w_typing_volatility: f32,
    pub w_click_rate: f32,
    pub w_error_signal: f32,
    pub w_idle: f32,             // negative-pressure: idle drops tension
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InputConfig {
    pub enable_keyboard_hook: bool,
    pub enable_mouse_hook: bool,
    pub mouse_move_min_interval_ms: u64,
    pub idle_threshold_secs: u32,
}

/// Maps an `AppCategory` name (lower-snake-case) to a list of executable
/// basenames (with `.exe` suffix on Windows). The `WindowTracker` walks
/// this map on every focus change to attach a coarse category to the
/// emitted `WindowChanged` event. Lookup is **case-insensitive** on the
/// executable name; the category key uses the canonical `AppCategory`
/// labels (see `AppCategory::as_str`).
///
/// Added in Fix 4 of the Module-O preparatory pass.
pub type AppCategoryMap = HashMap<String, Vec<String>>;

/// Default mapping. Users can override or extend via `config.toml`'s
/// `[perception.app_categories]` table.
pub fn default_app_categories() -> AppCategoryMap {
    let mut m: AppCategoryMap = HashMap::new();
    m.insert(
        "coding".into(),
        vec![
            "Code.exe".into(),
            "code.exe".into(),
            "idea64.exe".into(),
            "devenv.exe".into(),
            "cursor.exe".into(),
            "rider64.exe".into(),
            "pycharm64.exe".into(),
            "clion64.exe".into(),
            "sublime_text.exe".into(),
        ],
    );
    m.insert(
        "browser".into(),
        vec![
            "chrome.exe".into(),
            "firefox.exe".into(),
            "msedge.exe".into(),
            "brave.exe".into(),
            "opera.exe".into(),
            "arc.exe".into(),
        ],
    );
    m.insert(
        "terminal".into(),
        vec![
            "WindowsTerminal.exe".into(),
            "powershell.exe".into(),
            "pwsh.exe".into(),
            "wt.exe".into(),
            "alacritty.exe".into(),
            "cmd.exe".into(),
        ],
    );
    m.insert(
        "communication".into(),
        vec![
            "slack.exe".into(),
            "discord.exe".into(),
            "Teams.exe".into(),
            "Telegram.exe".into(),
            "zoom.exe".into(),
            "WhatsApp.exe".into(),
        ],
    );
    m.insert(
        "docs".into(),
        vec![
            "WINWORD.EXE".into(),
            "EXCEL.EXE".into(),
            "Notion.exe".into(),
            "obsidian.exe".into(),
            "POWERPNT.EXE".into(),
        ],
    );
    m.insert(
        "entertainment".into(),
        vec![
            "Spotify.exe".into(),
            "vlc.exe".into(),
            "Netflix.exe".into(),
            "Steam.exe".into(),
        ],
    );
    m
}

/// Phase 1, Module H — perception subsystem (metrics + window + screenshot).
///
/// All fields are individually documented because this is the surface a user
/// is going to edit by hand in `config.toml` to tune behaviour.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PerceptionConfig {
    // ---- Input metrics aggregator -----------------------------------------
    /// How often the metrics aggregator publishes a fresh `InputMetrics`
    /// snapshot, in milliseconds. Default 5 s.
    pub metrics_tick_ms: u64,
    /// Sliding-window length over which metrics are computed, in seconds.
    /// 60 s is the natural unit for WPM and rate-per-minute fields.
    pub metrics_window_secs: u32,
    /// Append a metrics snapshot to the Quantum Log every Nth tick. With
    /// the defaults (5 s tick, every 12th) that's once per minute.
    pub metrics_log_every_n_ticks: u64,
    /// Number of backspaces inside `backspace_storm_window_ms` that
    /// triggers `backspace_storm = true`. Default 5.
    pub backspace_storm_threshold: usize,
    /// Sliding window for the backspace-storm heuristic, in milliseconds.
    /// Default 3000 (5 backspaces in 3 s = storm).
    pub backspace_storm_window_ms: u64,

    // ---- Window tracker ---------------------------------------------------
    /// How often the foreground window is polled, in milliseconds. Lower =
    /// faster reaction to focus changes; higher = less CPU. Default 500.
    pub window_poll_ms: u64,
    /// App categorisation map (Fix 4). Used by `WindowTracker` to tag the
    /// foreground process. `#[serde(default = ...)]` ensures upgrades from
    /// Phase-0 / early-Phase-1 configs don't require manual migration.
    #[serde(default = "default_app_categories")]
    pub app_categories: AppCategoryMap,

    // ---- Screenshot capture -----------------------------------------------
    /// Periodic capture interval in seconds. `0` disables periodic captures
    /// (on-demand still works). Default 0 — opt-in because it costs disk.
    pub screenshot_interval_secs: u64,
    /// Maximum number of screenshots to keep on disk. Oldest are pruned.
    /// `0` = no retention (keep everything; user manages it). Default 200.
    pub screenshot_max_keep: usize,
}

impl PerceptionConfig {
    /// Case-insensitive lookup of an executable name in `app_categories`.
    /// Returns the matching [`ultron_types::AppCategory`] or `None` if the
    /// process is unknown to the config. The `WindowTracker` uses this on
    /// every focus change.
    pub fn classify_app(&self, process_name: &str) -> Option<ultron_types::AppCategory> {
        if process_name.is_empty() {
            return None;
        }
        let want = process_name.to_ascii_lowercase();
        for (category, exes) in &self.app_categories {
            for exe in exes {
                if exe.to_ascii_lowercase() == want {
                    return Some(ultron_types::AppCategory::from_str_lossy(category));
                }
            }
        }
        None
    }
}

/// Phase 1, Module O — Insight Pulse sidecar (`ultron-insight-pulse`).
/// The fields here are consumed by the sidecar process; the core daemon
/// only carries them so they live in the canonical config file alongside
/// everything else.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InsightConfig {
    /// How often `InsightSnapshot` is published, in seconds. Default 5.
    pub tick_secs: u64,
    /// Log `InsightTick` to the Quantum Log every Nth tick (default 12 →
    /// once per minute given a 5-s tick). Keeps the log readable while
    /// still giving an audit trail.
    pub log_every_n_ticks: u64,
    /// `cognitive_load` threshold above which `InsightFired` is appended.
    /// Default 0.75.
    pub cognitive_load_alert_threshold: f32,
    /// Seconds after which a visual label is considered stale and dropped
    /// from the published snapshot. Default 120.
    pub visual_label_max_age_secs: u32,
    /// Ollama LLaVA model name used by the Python sidecar. The Rust
    /// sidecar never invokes it directly; the field lives here so both
    /// sides read the same configuration.
    pub llava_model: String,
}

impl Default for InsightConfig {
    fn default() -> Self {
        Self {
            tick_secs: 5,
            log_every_n_ticks: 12,
            cognitive_load_alert_threshold: 0.75,
            visual_label_max_age_secs: 120,
            llava_model: "llava:7b".into(),
        }
    }
}

impl Default for Config {
    fn default() -> Self {
        let base = ultron_data_root();
        Self {
            general: GeneralConfig {
                user_name: "Priyanshu".into(),
                data_dir: base.join("data"),
                logs_dir: base.join("logs"),
                heartbeat_secs: 5,
            },
            bridge: BridgeConfig {
                bind: "127.0.0.1:9420".into(),
                token: random_token(48),
            },
            tension: TensionConfig {
                ewma_alpha: 0.18,
                decay_per_sec: 0.015,
                w_typing_volatility: 0.35,
                w_click_rate: 0.20,
                w_error_signal: 0.40,
                // Fix 10 — raised from 0.10. Calibration rationale:
                // 60 s of idle with `w_idle = 0.20` yields a -0.20 target
                // pull, enough to noticeably move a Loaded score toward
                // Neutral over a few seconds rather than persisting until
                // the decay term alone winds it down.
                w_idle: 0.20,
            },
            input: InputConfig {
                enable_keyboard_hook: true,
                enable_mouse_hook: true,
                mouse_move_min_interval_ms: 50,
                idle_threshold_secs: 90,
            },
            perception: PerceptionConfig::default(),
            insight: InsightConfig::default(),
        }
    }
}

impl Default for PerceptionConfig {
    fn default() -> Self {
        Self {
            metrics_tick_ms: 5_000,
            metrics_window_secs: 60,
            metrics_log_every_n_ticks: 12, // ~1 minute with 5s tick
            backspace_storm_threshold: 5,
            backspace_storm_window_ms: 3_000,
            window_poll_ms: 500,
            app_categories: default_app_categories(),
            screenshot_interval_secs: 0, // off by default
            screenshot_max_keep: 200,
        }
    }
}

impl Config {
    /// Load (or create-on-first-run) the config. Always returns absolute paths.
    pub fn load_or_create() -> CoreResult<Self> {
        let path = config_path();
        if !path.exists() {
            std::fs::create_dir_all(path.parent().unwrap())?;
            let cfg = Config::default();
            std::fs::create_dir_all(&cfg.general.data_dir)?;
            std::fs::create_dir_all(&cfg.general.logs_dir)?;
            let s = toml::to_string_pretty(&cfg)?;
            std::fs::write(&path, s)?;
            return Ok(cfg);
        }
        let s = std::fs::read_to_string(&path)?;
        let cfg: Config = toml::from_str(&s)?;
        std::fs::create_dir_all(&cfg.general.data_dir)?;
        std::fs::create_dir_all(&cfg.general.logs_dir)?;
        cfg.validate()?;
        Ok(cfg)
    }

    pub fn quantum_log_path(&self) -> PathBuf {
        self.general.data_dir.join("quantum.db")
    }

    fn validate(&self) -> CoreResult<()> {
        if self.bridge.token.len() < 16 {
            return Err(CoreError::Config(
                "bridge.token must be at least 16 chars".into(),
            ));
        }
        if !(0.0..=1.0).contains(&self.tension.ewma_alpha) {
            return Err(CoreError::Config("tension.ewma_alpha out of range".into()));
        }
        Ok(())
    }
}

pub fn ultron_data_root() -> PathBuf {
    if let Some(d) = dirs::data_dir() {
        d.join("ULTRON")
    } else {
        PathBuf::from(".ultron")
    }
}

pub fn config_path() -> PathBuf {
    ultron_data_root().join("config.toml")
}

/// Cryptographically-decent token. We don't need full CSPRNG vibes here —
/// the bridge port is `127.0.0.1` only — but more entropy is free.
fn random_token(len: usize) -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    let mut out = String::with_capacity(len);
    let mut state = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0xDEAD_BEEF);
    state ^= std::process::id() as u64;
    for _ in 0..len {
        // splitmix64
        state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^= z >> 31;
        let idx = (z % ALPHABET.len() as u64) as usize;
        out.push(ALPHABET[idx] as char);
    }
    out
}

#[allow(dead_code)]
fn _path_must_be_absolute(p: &Path) -> bool {
    p.is_absolute()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_unique_and_long() {
        let a = random_token(48);
        let b = random_token(48);
        assert_eq!(a.len(), 48);
        assert_eq!(b.len(), 48);
        assert_ne!(a, b);
    }

    #[test]
    fn default_config_validates() {
        let c = Config::default();
        assert!(c.validate().is_ok());
    }

    #[test]
    fn w_idle_calibration_is_0_20() {
        // Fix 10 — pin the calibration so we don't accidentally drift back.
        let c = Config::default();
        assert!((c.tension.w_idle - 0.20).abs() < 1e-6, "w_idle = {}", c.tension.w_idle);
    }

    #[test]
    fn classify_app_resolves_known_processes() {
        let p = PerceptionConfig::default();
        assert_eq!(p.classify_app("Code.exe"), Some(ultron_types::AppCategory::Coding));
        assert_eq!(p.classify_app("code.exe"), Some(ultron_types::AppCategory::Coding));
        assert_eq!(p.classify_app("CHROME.EXE"), Some(ultron_types::AppCategory::Browser));
        assert_eq!(p.classify_app("Teams.exe"), Some(ultron_types::AppCategory::Communication));
        assert_eq!(p.classify_app("Spotify.exe"), Some(ultron_types::AppCategory::Entertainment));
        assert_eq!(p.classify_app("unknown.exe"), None);
        assert_eq!(p.classify_app(""), None);
    }
}
