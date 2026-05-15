//! # WebSocket Bridge
//!
//! A minimal `axum` server on `127.0.0.1:9420` (configurable). Clients —
//! Python bridge, Tauri HUD, CLI tooling — connect, send a `hello` with the
//! shared token, and then stream events both ways.
//!
//! Wire protocol = JSON, types in `ultron-types::messages`.

use crate::config::Config;
use crate::event_bus::EventBus;
use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        State,
    },
    http::StatusCode,
    response::IntoResponse,
    routing::{any, get},
    Router,
};
use futures_util::{sink::SinkExt, stream::StreamExt};
use std::collections::HashSet;
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::net::TcpListener;
use tokio::sync::oneshot;
use tracing::{debug, info, warn};
use ultron_quantum_log::{EntryKind, NewEntry, QuantumLog};
use ultron_types::{EventEnvelope, UltronEvent, WsClientMessage, WsServerMessage};

#[derive(Clone)]
pub struct WsState {
    pub cfg: Arc<Config>,
    pub bus: EventBus,
    pub qlog: QuantumLog,
}

pub async fn run(state: WsState, shutdown_rx: oneshot::Receiver<()>) -> anyhow::Result<()> {
    let bind: SocketAddr = state
        .cfg
        .bridge
        .bind
        .parse()
        .map_err(|e| anyhow::anyhow!("invalid bridge.bind {}: {e}", state.cfg.bridge.bind))?;

    let app = Router::new()
        .route("/health", get(health))
        .route("/ws", any(ws_upgrade))
        .with_state(state);

    let listener = TcpListener::bind(bind).await?;
    info!(%bind, "ws bridge listening");
    axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            let _ = shutdown_rx.await;
            info!("ws server: shutdown signal received");
        })
        .await?;
    Ok(())
}

async fn health() -> impl IntoResponse {
    (StatusCode::OK, "ultron-core ok")
}

async fn ws_upgrade(
    State(state): State<WsState>,
    ws: WebSocketUpgrade,
) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_socket(socket, state))
}

async fn handle_socket(mut socket: WebSocket, state: WsState) {
    let session_id = uuid::Uuid::new_v4().to_string();
    debug!(%session_id, "ws client connected");

    // Phase 1: require Hello with token.
    let authed = wait_for_hello(&mut socket, &state, &session_id).await;
    let (mut role, mut filter) = match authed {
        Some((r, f)) => (r, f),
        None => return, // hello failed; socket already closed
    };

    let mut bus_rx = state.bus.subscribe();
    let (mut sink, mut stream) = socket.split();

    // Sender task: pushes bus events out, filtered.
    let session_id_for_sender = session_id.clone();
    let send_task = tokio::spawn(async move {
        loop {
            tokio::select! {
                evt = bus_rx.recv() => {
                    match evt {
                        Ok(e) => {
                            let (kind, payload, ts) = explode_event(&e);
                            if !filter.is_empty() && !filter.contains(&kind) {
                                continue;
                            }
                            let msg = WsServerMessage::Event { kind, payload, ts };
                            if let Ok(s) = serde_json::to_string(&msg) {
                                if sink.send(Message::Text(s)).await.is_err() {
                                    break;
                                }
                            }
                        }
                        Err(tokio::sync::broadcast::error::RecvError::Lagged(n)) => {
                            warn!(%session_id_for_sender, lag = n, "ws client lagged");
                            continue;
                        }
                        Err(_) => break,
                    }
                }
            }
        }
        let _ = sink.close().await;
    });

    // Receiver task — direct loop here so we can mutate `filter` and `role`.
    while let Some(Ok(msg)) = stream.next().await {
        match msg {
            Message::Text(t) => {
                match serde_json::from_str::<WsClientMessage>(&t) {
                    Ok(WsClientMessage::Subscribe { kinds }) => {
                        filter = kinds.into_iter().collect();
                        let _ = state
                            .qlog
                            .append_async(NewEntry::new(
                                EntryKind::Wire,
                                "ws_server",
                                serde_json::json!({
                                    "session_id": session_id,
                                    "role": role,
                                    "op": "subscribe",
                                    "kinds": filter.iter().cloned().collect::<Vec<_>>(),
                                }),
                            ))
                            .await;
                    }
                    Ok(WsClientMessage::Publish { kind, payload }) => {
                        let env = EventEnvelope::new(
                            format!("ws:{}", role),
                            kind,
                            payload,
                        );
                        state.bus.publish(UltronEvent::Custom(env.clone()));
                        let _ = state
                            .qlog
                            .append_async(NewEntry::new(
                                EntryKind::Wire,
                                "ws_server",
                                serde_json::to_value(&env).unwrap_or(serde_json::Value::Null),
                            ))
                            .await;
                    }
                    Ok(WsClientMessage::Ping) => { /* sender task handles event broadcast; no-op here */ }
                    Ok(WsClientMessage::Hello { .. }) => {
                        // Re-hello after auth is harmless — ignore.
                    }
                    Err(e) => {
                        warn!(%session_id, error = %e, "ws bad message: {}", t);
                    }
                }
            }
            Message::Close(_) => break,
            _ => {}
        }
    }
    let _ = send_task.await;
    debug!(%session_id, "ws client disconnected");
    // Reference the var so the compiler doesn't think it's only-write.
    let _ = &mut role;
}

/// Wait for the Hello message. Disconnects on any other message or on a bad
/// token. Returns (role, initial-filter=empty).
async fn wait_for_hello(
    socket: &mut WebSocket,
    state: &WsState,
    session_id: &str,
) -> Option<(String, HashSet<String>)> {
    // We give 5 seconds for the hello, then bail.
    let recv = tokio::time::timeout(std::time::Duration::from_secs(5), socket.recv()).await;
    let msg = match recv {
        Ok(Some(Ok(m))) => m,
        _ => {
            let _ = socket
                .send(Message::Text(
                    serde_json::to_string(&WsServerMessage::Error {
                        code: "hello_timeout".into(),
                        message: "expected hello within 5s".into(),
                    })
                    .unwrap_or_default(),
                ))
                .await;
            return None;
        }
    };
    let txt = match msg {
        Message::Text(t) => t,
        _ => return None,
    };
    let parsed: WsClientMessage = match serde_json::from_str(&txt) {
        Ok(p) => p,
        Err(e) => {
            let _ = socket
                .send(Message::Text(
                    serde_json::to_string(&WsServerMessage::Error {
                        code: "bad_hello".into(),
                        message: format!("{e}"),
                    })
                    .unwrap_or_default(),
                ))
                .await;
            return None;
        }
    };
    let WsClientMessage::Hello { token, role } = parsed else {
        let _ = socket
            .send(Message::Text(
                serde_json::to_string(&WsServerMessage::Error {
                    code: "expected_hello".into(),
                    message: "first message must be hello".into(),
                })
                .unwrap_or_default(),
            ))
            .await;
        return None;
    };
    if token != state.cfg.bridge.token {
        let _ = socket
            .send(Message::Text(
                serde_json::to_string(&WsServerMessage::Error {
                    code: "bad_token".into(),
                    message: "invalid bridge token".into(),
                })
                .unwrap_or_default(),
            ))
            .await;
        return None;
    }
    let welcome = WsServerMessage::Welcome {
        server_version: env!("CARGO_PKG_VERSION").to_string(),
        session_id: session_id.to_string(),
    };
    let _ = socket
        .send(Message::Text(
            serde_json::to_string(&welcome).unwrap_or_default(),
        ))
        .await;
    let _ = state
        .qlog
        .append_async(NewEntry::new(
            EntryKind::Wire,
            "ws_server",
            serde_json::json!({
                "op": "hello",
                "role": role,
                "session_id": session_id,
            }),
        ))
        .await;
    Some((role, HashSet::new()))
}

/// Convert an UltronEvent into (kind-string, payload-value, ts).
fn explode_event(e: &UltronEvent) -> (String, serde_json::Value, chrono::DateTime<chrono::Utc>) {
    let now = chrono::Utc::now();
    match e {
        UltronEvent::Heartbeat { tension, uptime_secs } => (
            "heartbeat".into(),
            serde_json::json!({ "tension": tension, "uptime_secs": uptime_secs }),
            now,
        ),
        UltronEvent::InputActivity(sig) => (
            "input_activity".into(),
            serde_json::to_value(sig).unwrap_or(serde_json::Value::Null),
            now,
        ),
        UltronEvent::InputMetricsUpdated(m) => (
            "input_metrics_updated".into(),
            serde_json::to_value(m).unwrap_or(serde_json::Value::Null),
            now,
        ),
        UltronEvent::WindowChanged {
            title,
            process_name,
            pid,
            hwnd,
            app_category,
            ts_unix_ms,
        } => (
            "window_changed".into(),
            serde_json::json!({
                "title": title,
                "process_name": process_name,
                "pid": pid,
                "hwnd": hwnd,
                "app_category": app_category.as_ref().map(|c| c.as_str()),
                "ts_unix_ms": ts_unix_ms,
            }),
            now,
        ),
        UltronEvent::ScreenshotCaptured {
            path,
            width,
            height,
            reason,
            ts_unix_ms,
        } => (
            "screenshot_captured".into(),
            serde_json::json!({
                "path": path,
                "width": width,
                "height": height,
                "reason": reason,
                "ts_unix_ms": ts_unix_ms,
            }),
            now,
        ),
        UltronEvent::TensionChanged { previous, current } => (
            "tension_changed".into(),
            serde_json::json!({ "previous": previous, "current": current }),
            now,
        ),
        UltronEvent::ServiceState { state: s } => (
            "service_state".into(),
            serde_json::json!({ "state": s }),
            now,
        ),
        UltronEvent::Custom(env) => (env.kind.clone(), env.payload.clone(), env.ts),
    }
}
