//! Reconnecting WebSocket client for the ultron-core bridge.
//!
//! Mirrors `python/ultron_bridge.py` so both sidecars present the same
//! behaviour to the daemon: exponential backoff reconnect, `op: hello`
//! handshake with token + role, optional `op: subscribe` filter, and an
//! async event handler invoked for each `op: event` frame.
//!
//! The handler receives the **wire JSON** (a `serde_json::Value`) rather
//! than typed events, because the bridge's `kind` field gets mapped onto
//! several different inner shapes and pre-decoding into a single Rust
//! enum would force us to mirror the daemon's `explode_event` table.

use anyhow::{anyhow, Context, Result};
use futures::SinkExt;
use futures_util::StreamExt;
use std::sync::Arc;
use std::time::Duration;
use tokio::net::TcpStream;
use tokio::sync::Mutex;
use tokio_tungstenite::tungstenite::{client::IntoClientRequest, Message};
use tokio_tungstenite::{connect_async, MaybeTlsStream, WebSocketStream};
use tracing::{debug, info, warn};

/// Tunable cap for incoming frames. The daemon's snapshots are small
/// (~1 KB) and screenshot paths are bounded by the filesystem; 8 MiB is
/// already overkill but matches what `ultron_bridge.py` uses so the two
/// behave identically.
const MAX_FRAME_BYTES: usize = 8 * 1024 * 1024;

/// Exponential backoff schedule. Capped at the last value.
const BACKOFF_SECS: &[u64] = &[1, 2, 4, 8, 16, 30];

type WsStream = WebSocketStream<MaybeTlsStream<TcpStream>>;

#[derive(Clone, Debug)]
pub struct WsConfig {
    pub url: String,
    pub token: String,
    pub role: String,
    /// Empty = subscribe to all event kinds.
    pub subscribe_to: Vec<String>,
    pub handshake_timeout_secs: u64,
}

impl WsConfig {
    pub fn new(url: impl Into<String>, token: impl Into<String>) -> Self {
        Self {
            url: url.into(),
            token: token.into(),
            role: "memory-engine".into(),
            subscribe_to: vec![
                // Module D only cares about derived insight events plus
                // visual labels. We deliberately do NOT subscribe to
                // input_activity / heartbeat — they'd flood the DB.
                "insight_snapshot".into(),
                "visual_label".into(),
            ],
            handshake_timeout_secs: 10,
        }
    }
}

/// Handle the caller uses to publish events back to the daemon, regardless
/// of which underlying connection is currently live. Cloning is cheap.
#[derive(Clone, Default)]
pub struct WsHandle {
    inner: Arc<Mutex<Option<futures::stream::SplitSink<WsStream, Message>>>>,
}

impl WsHandle {
    /// Publish a `custom` event onto the daemon's bus. Returns `Ok(true)`
    /// if the frame was sent, `Ok(false)` if there's no live connection,
    /// `Err` only for serialisation failures.
    pub async fn publish_custom(
        &self,
        kind: &str,
        payload: serde_json::Value,
    ) -> Result<bool> {
        let frame = serde_json::json!({
            "op": "publish",
            "kind": kind,
            "payload": payload,
        });
        let text = serde_json::to_string(&frame)
            .context("serialise custom publish frame")?;
        let mut guard = self.inner.lock().await;
        let Some(sink) = guard.as_mut() else {
            return Ok(false);
        };
        if let Err(e) = sink.send(Message::Text(text)).await {
            warn!("publish_custom failed: {e}");
            // Drop the dead sink so the next reconnect installs a fresh one.
            *guard = None;
            return Ok(false);
        }
        Ok(true)
    }
}

/// Async event handler — receives parsed JSON for every `op: event` frame.
pub type EventCallback =
    Arc<dyn Fn(serde_json::Value) -> futures::future::BoxFuture<'static, ()> + Send + Sync>;

/// Run the reconnect loop forever. Returns only when shutdown is signalled
/// upstream (the future is dropped or cancelled).
pub async fn run_forever(cfg: WsConfig, handle: WsHandle, on_event: EventCallback) {
    let mut attempt: usize = 0;
    loop {
        match connect_once(&cfg, &handle, &on_event).await {
            Ok(()) => {
                // Clean disconnect — reset backoff and try again immediately
                // (modulo a 1s sanity sleep to avoid hot-spinning).
                attempt = 0;
                tokio::time::sleep(Duration::from_secs(1)).await;
            }
            Err(e) => {
                let delay = BACKOFF_SECS[attempt.min(BACKOFF_SECS.len() - 1)];
                warn!(
                    delay_secs = delay,
                    attempt,
                    "ws connection failed: {e:#} — reconnecting"
                );
                tokio::time::sleep(Duration::from_secs(delay)).await;
                attempt = attempt.saturating_add(1);
            }
        }
    }
}

/// Open one connection and pump it until it dies.
async fn connect_once(
    cfg: &WsConfig,
    handle: &WsHandle,
    on_event: &EventCallback,
) -> Result<()> {
    info!(url = %cfg.url, role = %cfg.role, "ws connecting");

    let req = cfg
        .url
        .as_str()
        .into_client_request()
        .with_context(|| format!("invalid ws url: {}", cfg.url))?;

    let (stream, _resp) = connect_async(req).await.context("ws connect")?;
    let (mut sink, mut source) = stream.split();

    // ---- handshake -------------------------------------------------------
    let hello = serde_json::json!({
        "op": "hello",
        "token": cfg.token,
        "role": cfg.role,
    });
    sink.send(Message::Text(hello.to_string()))
        .await
        .context("send hello")?;

    let welcome = tokio::time::timeout(
        Duration::from_secs(cfg.handshake_timeout_secs),
        source.next(),
    )
    .await
    .map_err(|_| anyhow!("no welcome within {}s", cfg.handshake_timeout_secs))?
    .ok_or_else(|| anyhow!("connection closed during handshake"))?
    .context("read welcome")?;

    let welcome_text = match welcome {
        Message::Text(t) => t,
        Message::Binary(b) => String::from_utf8_lossy(&b).into_owned(),
        other => return Err(anyhow!("unexpected welcome frame: {other:?}")),
    };
    let welcome_val: serde_json::Value =
        serde_json::from_str(&welcome_text).context("parse welcome")?;
    if welcome_val.get("op").and_then(|v| v.as_str()) != Some("welcome") {
        return Err(anyhow!("expected welcome, got {welcome_val}"));
    }
    info!(
        server_version = ?welcome_val.get("server_version"),
        session_id = ?welcome_val.get("session_id"),
        "ws handshake ok"
    );

    // ---- subscribe (optional) -------------------------------------------
    if !cfg.subscribe_to.is_empty() {
        let sub = serde_json::json!({
            "op": "subscribe",
            "kinds": cfg.subscribe_to,
        });
        sink.send(Message::Text(sub.to_string()))
            .await
            .context("send subscribe")?;
    }

    // Install the sink in the shared handle now that handshake is done so
    // callers can publish back. We deliberately *do not* hold the handle
    // mutex across the receive loop — only while swapping in/out.
    {
        let mut g = handle.inner.lock().await;
        *g = Some(sink);
    }

    // ---- receive loop ----------------------------------------------------
    let receive_result = receive_loop(&mut source, on_event).await;

    // Tear down: drop the sink so future publishes return Ok(false).
    {
        let mut g = handle.inner.lock().await;
        *g = None;
    }

    receive_result
}

async fn receive_loop(
    source: &mut futures::stream::SplitStream<WsStream>,
    on_event: &EventCallback,
) -> Result<()> {
    while let Some(frame) = source.next().await {
        let frame = frame.context("ws recv")?;
        let text = match frame {
            Message::Text(t) => t,
            Message::Binary(b) => String::from_utf8_lossy(&b).into_owned(),
            Message::Ping(_) | Message::Pong(_) => continue,
            Message::Close(_) => {
                info!("ws server closed");
                return Ok(());
            }
            Message::Frame(_) => continue,
        };
        if text.len() > MAX_FRAME_BYTES {
            warn!(len = text.len(), "oversized frame dropped");
            continue;
        }
        let msg: serde_json::Value = match serde_json::from_str(&text) {
            Ok(v) => v,
            Err(e) => {
                warn!("non-json frame: {e} :: {}", &text[..text.len().min(120)]);
                continue;
            }
        };
        match msg.get("op").and_then(|v| v.as_str()) {
            Some("event") => {
                // Dispatch — handler is responsible for not panicking.
                on_event(msg).await;
            }
            Some("ack") => debug!("ack: {msg}"),
            Some("error") => warn!("server error frame: {msg}"),
            Some("pong") => continue,
            _ => debug!("unhandled frame: {msg}"),
        }
    }
    Ok(())
}
