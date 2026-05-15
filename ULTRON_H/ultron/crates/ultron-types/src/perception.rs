//! Perception payloads.
//!
//! Types produced by Phase 1 — Module H (Screen + Enhanced Input Engine).
//! Like everything else in `ultron-types`, these have **zero IO and no
//! platform dependencies** — they are wire types and bus payloads only.
//!
//! The companion event variants live in [`crate::events::UltronEvent`]:
//! - `UltronEvent::InputMetricsUpdated(InputMetrics)`
//! - `UltronEvent::WindowChanged { .. }`
//! - `UltronEvent::ScreenshotCaptured { .. }`

use serde::{Deserialize, Serialize};

/// Periodically-computed metrics summarising a recent window of input
/// activity. Emitted by the perception subsystem on a configurable tick
/// (default: every 5 s; window size: 60 s).
///
/// All rates are normalised to **per-minute** so they're directly comparable
/// regardless of the window length.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct InputMetrics {
    /// Words-per-minute estimate. Counts character-producing keys
    /// (letters, digits, symbols, whitespace) in the window and divides by 5
    /// (the conventional average word length), then scales to per-minute.
    pub wpm: f32,

    /// Backspaces per minute over the window.
    pub backspace_rate_per_min: f32,

    /// `true` when a backspace storm is currently active — defined as
    /// `>= storm_threshold` backspaces inside `storm_window_ms` (config).
    /// Primary signal for "user is fighting their own typing".
    pub backspace_storm: bool,

    /// Coefficient of variation of inter-keystroke intervals over the
    /// window. `0.0` = perfectly metronomic; rises with hesitation /
    /// distraction. Compressed to `[0, 1]` (cv > 1.5 saturates).
    pub typing_rhythm_variance: f32,

    /// Average mouse cursor speed in pixels per second over the window.
    pub mouse_velocity_px_per_sec: f32,

    /// Heuristic `[0, 1]` for cursor wandering / hesitation. Higher = more
    /// direction reversals per unit time, which usually means the user is
    /// hovering over options instead of acting.
    pub mouse_hesitation_score: f32,

    /// Mouse-button presses (any button) per minute.
    pub click_rate_per_min: f32,

    /// Foreground-window switches per minute. Proxy for context-thrashing.
    pub app_switch_per_min: f32,

    /// Seconds since the last input event of any kind.
    pub idle_secs: f32,

    /// Length of the rolling window these metrics were computed over,
    /// in seconds. Useful for clients that want raw counts back.
    pub window_secs: f32,

    /// When this snapshot was produced (Unix epoch ms).
    pub ts_unix_ms: i64,
}

impl Default for InputMetrics {
    fn default() -> Self {
        Self {
            wpm: 0.0,
            backspace_rate_per_min: 0.0,
            backspace_storm: false,
            typing_rhythm_variance: 0.0,
            mouse_velocity_px_per_sec: 0.0,
            mouse_hesitation_score: 0.0,
            click_rate_per_min: 0.0,
            app_switch_per_min: 0.0,
            idle_secs: 0.0,
            window_secs: 60.0,
            ts_unix_ms: 0,
        }
    }
}

/// Snapshot of the foreground window. Published only when the window
/// **changes** — repeated polls of the same window do not re-fire the event.
///
/// Phase 1 publishes the raw title locally so dependent modules (Insight
/// Pulse, Ghost Network) can hash or summarise it. **The Privacy Router
/// from Phase 4 governs anything that leaves the machine.**
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct WindowInfo {
    /// Window title as reported by `GetWindowTextW`. Possibly empty.
    pub title: String,
    /// Process executable name (e.g. `"Code.exe"`). Best effort —
    /// some processes block `OpenProcess` queries even with limited rights.
    pub process_name: String,
    /// Owning process id.
    pub pid: u32,
    /// Raw `HWND` cast to `i64` (Win32 handles fit; this is just for tracing
    /// and de-duplication, never to be dereferenced from anywhere but the
    /// originating process).
    pub hwnd: i64,
    /// When the change was observed (Unix epoch ms).
    pub ts_unix_ms: i64,
}

/// Why a screenshot was captured. Lets downstream consumers (LLaVA, Insight
/// Pulse, Privacy Router) decide how aggressively to process / retain it.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ScreenshotReason {
    /// Routine periodic capture from the configured interval.
    Periodic,
    /// Explicit caller request (e.g. another module via the bus).
    OnDemand,
    /// Tension crossed into the high band — capture for context.
    HighTension,
    /// Active foreground window changed.
    WindowChange,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn input_metrics_serde() {
        let m = InputMetrics {
            wpm: 72.5,
            backspace_storm: true,
            ..InputMetrics::default()
        };
        let v: serde_json::Value = serde_json::to_value(&m).unwrap();
        assert_eq!(v["wpm"], 72.5);
        assert_eq!(v["backspace_storm"], true);
        let back: InputMetrics = serde_json::from_value(v).unwrap();
        assert_eq!(back, m);
    }

    #[test]
    fn screenshot_reason_serde() {
        let r = ScreenshotReason::HighTension;
        let s = serde_json::to_string(&r).unwrap();
        assert_eq!(s, "\"high_tension\"");
    }

    #[test]
    fn window_info_serde() {
        let w = WindowInfo {
            title: "main.rs - ULTRON - Visual Studio Code".into(),
            process_name: "Code.exe".into(),
            pid: 1234,
            hwnd: 0x12345,
            ts_unix_ms: 1_700_000_000_000,
        };
        let v: serde_json::Value = serde_json::to_value(&w).unwrap();
        assert_eq!(v["pid"], 1234);
        assert_eq!(v["process_name"], "Code.exe");
    }
}
