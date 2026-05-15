//! Ghost Network payloads (Phase 1, Module Q).
//!
//! Wire types for cross-device presence sync over LAN. Like everything else
//! in `ultron-types`, these have **zero IO and no platform dependencies** —
//! they are bus payloads and serialised network messages only.
//!
//! ## Schema versioning
//!
//! Every `GhostState` carries a `schema_version`. When we evolve the schema,
//! receivers compare against their own and either upgrade, downgrade, or
//! drop the message. Phase 1 ships v1.

use serde::{Deserialize, Serialize};

/// Direction of recent tension change. Computed from a short rolling
/// history (≈60 s) by comparing the last 10 s mean against the older
/// 10–60 s mean.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TensionTrend {
    Rising,
    Falling,
    Stable,
}

impl Default for TensionTrend {
    fn default() -> Self {
        Self::Stable
    }
}

/// State broadcast by an ULTRON instance to its LAN peers, encrypted on
/// the wire with AES-256-GCM.
///
/// **Privacy:** raw window titles never leave the machine. We send a
/// `blake3` hash so a peer can detect "we're on the same browser tab"
/// without learning what tab. The Privacy Router (Phase 4) will further
/// gate the `active_context` field, which is currently best-effort
/// derived from `WindowChanged`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct GhostState {
    /// Sender's stable per-machine UUID. Used for self-dedup on receivers.
    pub device_id: String,
    /// Human-readable device name (e.g. `"LAPTOP-HM36HMQC"`).
    pub device_name: String,
    /// Latest local tension score in `[0.0, 1.0]`.
    pub tension: f32,
    /// Direction of recent change.
    pub tension_trend: TensionTrend,
    /// Short, human-readable summary of the user's current activity.
    /// Phase 1: derived from the foreground window. Later phases may
    /// override via dedicated context-tracking modules.
    pub active_context: String,
    /// `blake3(active window title)` as lowercase hex (64 chars).
    pub active_window_hash: String,
    /// `blake3(latest screenshot path)` as lowercase hex.
    /// A different hash = the screen has likely changed; same hash for a
    /// long time = static screen. Phase 6 may upgrade to perceptual hashing.
    pub screen_state_hash: String,
    /// When the sender last produced its own heartbeat, in Unix epoch ms.
    pub last_heartbeat_ms: i64,
    /// Names of the modules this instance is exposing
    /// (e.g. `["input", "screen", "ghost"]`).
    pub capabilities: Vec<String>,
    /// When this snapshot was assembled, in Unix epoch ms. Distinct from
    /// `last_heartbeat_ms`, which can lag this by up to one heartbeat.
    pub ts_unix_ms: i64,
    /// Schema revision. Receivers compare against their own and either
    /// upgrade, downgrade, or drop the message.
    pub schema_version: u32,
}

/// Schema version of the current `GhostState` definition.
pub const GHOST_SCHEMA_VERSION: u32 = 1;

/// Summary of a discovered ULTRON instance on the LAN. Published with
/// [`crate::events::UltronEvent::GhostDeviceDiscovered`].
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct GhostPeer {
    pub device_id: String,
    pub device_name: String,
    /// `ip:port` string form so it round-trips JSON cleanly.
    pub addr: String,
    pub capabilities: Vec<String>,
    pub ts_unix_ms: i64,
}

/// The actual on-the-wire frame exchanged between ghost-network peers.
///
/// This is what gets AES-GCM encrypted and pushed across TCP. Distinct
/// from `GhostState` (which is a richer state snapshot for future use):
/// `GhostFrame` is a thin envelope around any local event that we've
/// chosen to export onto the LAN.
///
/// ## Privacy
///
/// Before serialisation, the publisher runs the `payload` through the
/// scrubber. Sensitive fields (window titles, process names) are
/// replaced with BLAKE3 digests so receivers can correlate ("the
/// foreground app on peer X changed") without learning content.
///
/// `sender_id` is `BLAKE3(hostname + ghost_secret)` — opaque, stable
/// per machine, identical across restarts of the same configured node.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct GhostFrame {
    /// Opaque stable per-machine ID. Receivers use this to de-duplicate
    /// and to filter out their own echoes if the network ever loops.
    pub sender_id: String,
    /// Mirrors the WS bridge `kind` string. Receivers re-publish onto
    /// their own local bus with this kind verbatim.
    pub kind: String,
    /// When the sender produced the frame, Unix epoch ms (UTC).
    pub ts: i64,
    /// Scrubbed payload — sensitive fields already hashed by the
    /// publisher's scrubber before this struct was assembled.
    pub payload: serde_json::Value,
}

impl GhostFrame {
    /// Wire `kind` of the WS `op:publish` envelope the listener uses to
    /// re-inject a received frame onto the local bus. Receivers prepend
    /// this prefix so consumers can distinguish remote from local
    /// events: a remote `insight_snapshot` arrives as
    /// `ghost:insight_snapshot`.
    pub const REMOTE_PREFIX: &'static str = "ghost:";
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ghost_state_roundtrip() {
        let s = GhostState {
            device_id: "abc123".into(),
            device_name: "Laptop".into(),
            tension: 0.42,
            tension_trend: TensionTrend::Rising,
            active_context: "Code — main.rs".into(),
            active_window_hash: "deadbeef".into(),
            screen_state_hash: "feedface".into(),
            last_heartbeat_ms: 1_700_000_000_000,
            capabilities: vec!["input".into(), "screen".into()],
            ts_unix_ms: 1_700_000_000_500,
            schema_version: GHOST_SCHEMA_VERSION,
        };
        let v = serde_json::to_string(&s).unwrap();
        let back: GhostState = serde_json::from_str(&v).unwrap();
        assert_eq!(s, back);
    }

    #[test]
    fn tension_trend_serde() {
        assert_eq!(serde_json::to_string(&TensionTrend::Rising).unwrap(), "\"rising\"");
        assert_eq!(serde_json::to_string(&TensionTrend::Falling).unwrap(), "\"falling\"");
        assert_eq!(serde_json::to_string(&TensionTrend::Stable).unwrap(), "\"stable\"");
    }

    #[test]
    fn ghost_peer_roundtrip() {
        let p = GhostPeer {
            device_id: "xyz".into(),
            device_name: "Desk".into(),
            addr: "192.168.1.5:9421".into(),
            capabilities: vec!["ghost".into()],
            ts_unix_ms: 1,
        };
        let v = serde_json::to_value(&p).unwrap();
        assert_eq!(v["addr"], "192.168.1.5:9421");
    }

    #[test]
    fn ghost_frame_roundtrip() {
        let f = GhostFrame {
            sender_id: "deadbeefcafebabe".into(),
            kind: "insight_snapshot".into(),
            ts: 1_700_000_000_000,
            payload: serde_json::json!({"tension": 0.42, "tick": 5}),
        };
        let bytes = serde_json::to_vec(&f).unwrap();
        let back: GhostFrame = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(f, back);
        assert_eq!(back.payload["tension"], 0.42);
    }

    #[test]
    fn ghost_frame_remote_prefix_const() {
        assert_eq!(GhostFrame::REMOTE_PREFIX, "ghost:");
    }
}
