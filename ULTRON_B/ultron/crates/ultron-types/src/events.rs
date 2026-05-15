use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// Generic event envelope used when modules want to publish ad-hoc events
/// that aren't yet first-class variants of [`UltronEvent`].
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EventEnvelope {
    pub id: Uuid,
    pub ts: DateTime<Utc>,
    pub source: String,
    pub kind: String,
    pub payload: serde_json::Value,
}

impl EventEnvelope {
    pub fn new(
        source: impl Into<String>,
        kind: impl Into<String>,
        payload: serde_json::Value,
    ) -> Self {
        Self {
            id: Uuid::new_v4(),
            ts: Utc::now(),
            source: source.into(),
            kind: kind.into(),
            payload,
        }
    }
}

/// First-class events broadcast on the core bus.
///
/// New variants should be added rather than overloading [`UltronEvent::Custom`]
/// once a type stabilises.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum UltronEvent {
    /// Emitted every N seconds by the core. Useful liveness probe.
    Heartbeat {
        tension: f32,
        uptime_secs: u64,
    },
    /// Privacy-respecting **raw** input metadata, one per input signal.
    InputActivity(InputSignal),
    /// **Computed** input metrics (WPM, mouse velocity, etc.), emitted
    /// periodically by the perception subsystem (Phase 1, Module H).
    InputMetricsUpdated(crate::perception::InputMetrics),
    /// Foreground window changed. Title and process_name are local-only
    /// until the Privacy Router (Phase 4) approves any outbound use.
    WindowChanged {
        title: String,
        process_name: String,
        pid: u32,
        hwnd: i64,
        ts_unix_ms: i64,
    },
    /// A screenshot was captured to disk. Path is absolute. Future modules
    /// (LLaVA, Insight Pulse) consume this to attach visual context.
    ScreenshotCaptured {
        path: String,
        width: u32,
        height: u32,
        reason: crate::perception::ScreenshotReason,
        ts_unix_ms: i64,
    },
    /// Threshold-crossing transitions for the tension score.
    TensionChanged {
        previous: f32,
        current: f32,
    },
    /// Daemon lifecycle.
    ServiceState {
        state: String,
    },
    /// Anything else, until promoted to a real variant.
    Custom(EventEnvelope),
}

/// Privacy-conscious input signal. **Never** carries actual keystrokes —
/// only categorical metadata, modifier mask, timing.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum InputSignal {
    KeyEvent {
        ts_ms: i64,
        category: KeyCategory,
        modifier_mask: u8,
        is_down: bool,
    },
    MouseMove {
        ts_ms: i64,
        dx: i32,
        dy: i32,
    },
    MouseButton {
        ts_ms: i64,
        button: MouseButton,
        is_down: bool,
    },
    MouseScroll {
        ts_ms: i64,
        /// 120 = one notch up, -120 = one notch down (Win32 convention).
        delta: i32,
    },
    /// User idle for >= [`InputSignal::Idle::secs`] seconds. Emitted once on entry.
    Idle {
        ts_ms: i64,
        secs: u32,
    },
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
pub enum KeyCategory {
    Letter,
    Digit,
    Symbol,
    Whitespace, // space, tab, enter
    Backspace,  // tracked separately — primary error-rate signal
    Modifier,   // shift / ctrl / alt / win
    Navigation, // arrows, home, end, pgup/pgdn, ins, del
    Function,   // F1..F24
    System,     // esc, capslock, prtscn, scrolllock, pause
    Unknown,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "snake_case")]
pub enum MouseButton {
    Left,
    Right,
    Middle,
    X1,
    X2,
}

/// Bit positions for [`InputSignal::KeyEvent::modifier_mask`].
pub mod modifier_bits {
    pub const SHIFT: u8 = 1 << 0;
    pub const CTRL:  u8 = 1 << 1;
    pub const ALT:   u8 = 1 << 2;
    pub const WIN:   u8 = 1 << 3;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_key_event() {
        let s = InputSignal::KeyEvent {
            ts_ms: 1234,
            category: KeyCategory::Letter,
            modifier_mask: modifier_bits::CTRL | modifier_bits::SHIFT,
            is_down: true,
        };
        let json = serde_json::to_string(&s).unwrap();
        let back: InputSignal = serde_json::from_str(&json).unwrap();
        assert_eq!(format!("{back:?}"), format!("{s:?}"));
    }

    #[test]
    fn ultron_event_serde_tag() {
        let e = UltronEvent::Heartbeat { tension: 0.42, uptime_secs: 99 };
        let v: serde_json::Value = serde_json::to_value(&e).unwrap();
        assert_eq!(v["type"], "heartbeat");
        assert_eq!(v["tension"], 0.42);
    }

    #[test]
    fn window_changed_serde_tag() {
        let e = UltronEvent::WindowChanged {
            title: "Code.exe".into(),
            process_name: "Code.exe".into(),
            pid: 4242,
            hwnd: 0xDEAD,
            ts_unix_ms: 1_700_000_000_000,
        };
        let v: serde_json::Value = serde_json::to_value(&e).unwrap();
        assert_eq!(v["type"], "window_changed");
        assert_eq!(v["pid"], 4242);
    }

    #[test]
    fn screenshot_captured_serde_tag() {
        let e = UltronEvent::ScreenshotCaptured {
            path: r"C:\foo\a.png".into(),
            width: 1920,
            height: 1080,
            reason: crate::perception::ScreenshotReason::Periodic,
            ts_unix_ms: 1,
        };
        let v: serde_json::Value = serde_json::to_value(&e).unwrap();
        assert_eq!(v["type"], "screenshot_captured");
        assert_eq!(v["reason"], "periodic");
    }
}
