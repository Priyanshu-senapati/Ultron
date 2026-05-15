use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// Messages the **client** (Python bridge, Tauri HUD, CLI) sends to the core.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum WsClientMessage {
    /// First message; carries the shared-secret token. Required.
    Hello {
        token: String,
        role: String, // e.g. "python-bridge", "tauri-hud", "cli"
    },
    /// Limit which event kinds will be pushed to this client.
    /// Empty list = subscribe to all.
    Subscribe {
        kinds: Vec<String>,
    },
    /// Inject an event onto the bus from a client (e.g. Python module emits).
    Publish {
        kind: String,
        payload: serde_json::Value,
    },
    /// Liveness probe.
    Ping,
}

/// Messages the **core** sends to clients.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum WsServerMessage {
    Welcome {
        server_version: String,
        session_id: String,
    },
    Event {
        kind: String,
        payload: serde_json::Value,
        ts: DateTime<Utc>,
    },
    Ack {
        ok: bool,
        msg: Option<String>,
    },
    Error {
        code: String,
        message: String,
    },
    Pong,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hello_serde() {
        let m = WsClientMessage::Hello {
            token: "secret".into(),
            role: "python-bridge".into(),
        };
        let s = serde_json::to_string(&m).unwrap();
        assert!(s.contains("\"op\":\"hello\""));
        let back: WsClientMessage = serde_json::from_str(&s).unwrap();
        match back {
            WsClientMessage::Hello { token, role } => {
                assert_eq!(token, "secret");
                assert_eq!(role, "python-bridge");
            }
            _ => panic!("wrong variant"),
        }
    }
}
