//! `ultron-ghost` — Module Q Ghost Network runtime.
//!
//! Five concurrent subsystems all sharing the ultron-core WS bridge as
//! the local-event source/sink:
//!
//! 1. **WS client (in)** — subscribes to `cfg.ghost.export_kinds`,
//!    forwards every received event to the publisher.
//! 2. **Publisher** — scrubs sensitive fields, encrypts, fans the
//!    encrypted frame out to each currently-connected peer.
//! 3. **Discovery** — mDNS advertise + browse; mutates the shared
//!    `PeerMap`.
//! 4. **Listener** — TCP accept loop; decrypts incoming frames into
//!    `RemoteEvent`s and pushes them onto an mpsc channel.
//! 5. **WS client (out)** — drains the inbound mpsc channel and
//!    republishes each `RemoteEvent` onto the local bus with a
//!    `"ghost:"` prefix.
//!
//! On Ctrl-C: flip the shutdown flag, abort every JoinHandle, await
//! each, exit clean.

mod config;
mod crypto;
mod discovery;
mod listener;
mod peer_map;
mod publisher;
mod scrubber;
mod ws_client;

use anyhow::{Context, Result};
use std::sync::Arc;
use tokio::sync::mpsc;
use tracing::{info, warn};
use ultron_quantum_log::{EntryKind, NewEntry, QuantumLog};

use crate::config::GhostMainConfig;
use crate::crypto::{compute_sender_id, GhostCipher};
use crate::discovery::Discovery;
use crate::listener::{Listener, RemoteEvent};
use crate::peer_map::PeerMap;
use crate::publisher::Publisher;
use crate::ws_client::{EventCallback, WsConfig, WsHandle};

const VERSION: &str = env!("CARGO_PKG_VERSION");
const MODULE: &str = "ghost-network";

#[tokio::main]
async fn main() -> Result<()> {
    init_tracing();
    info!(version = VERSION, "ultron-ghost starting");

    // ---- Config + secrets ----------------------------------------------
    let path = config::config_path()?;
    let mut cfg: GhostMainConfig = config::load(&path)?;
    config::ensure_secrets(&path, &mut cfg)?;
    if !cfg.ghost.enabled {
        info!("[ghost] enabled = false in config — exiting cleanly");
        return Ok(());
    }

    // ---- Identity ------------------------------------------------------
    let hostname = hostname::get()
        .map(|h| h.to_string_lossy().into_owned())
        .unwrap_or_else(|_| "unknown".into());
    let sender_id = compute_sender_id(&hostname, &cfg.ghost.ghost_secret);
    info!(host = %hostname, sender_id = %sender_id, "identity ready");

    // ---- Crypto --------------------------------------------------------
    let cipher = GhostCipher::from_secret(&cfg.ghost.ghost_secret)?;

    // ---- Quantum Log ---------------------------------------------------
    let qlog_path = cfg.general.data_dir.join("quantum.db");
    let qlog = QuantumLog::open(&qlog_path)
        .with_context(|| format!("open quantum log at {}", qlog_path.display()))?;
    qlog.append(NewEntry::new(
        EntryKind::Boot,
        MODULE,
        serde_json::json!({
            "version": VERSION,
            "sender_id": sender_id,
            "instance_id": cfg.ghost.instance_id,
            "port": cfg.ghost.port,
        }),
    ))
    .ok();

    // ---- Peer map (shared across discovery, publisher) ----------------
    let peers = PeerMap::new();

    // ---- Inbound channel: listener → WS-out task ----------------------
    let (inbound_tx, mut inbound_rx) = mpsc::channel::<RemoteEvent>(256);

    // ---- TCP listener --------------------------------------------------
    let listener = Listener::new(
        cipher.clone(),
        sender_id.clone(),
        inbound_tx,
        qlog.clone(),
    );
    let (bound_addr, listener_task) = listener
        .clone()
        .spawn(cfg.ghost.port)
        .await
        .context("bind ghost listener")?;
    info!(addr = %bound_addr, "ghost listener live");

    // If the user configured port=0 (OS-assigned), the mDNS
    // advertisement needs the actual port. Substitute the real one.
    let advertised_port = bound_addr.port();

    // ---- mDNS discovery ------------------------------------------------
    let discovery = Discovery::start(
        &cfg.ghost.instance_id,
        advertised_port,
        &sender_id,
        peers.clone(),
    )
    .context("start mdns discovery")?;

    // ---- Publisher + supervisor ----------------------------------------
    let publisher = Publisher::new(
        cipher.clone(),
        sender_id.clone(),
        &cfg.ghost,
        peers.clone(),
        qlog.clone(),
    );
    let publisher_supervisor = publisher.clone().spawn_supervisor();

    // ---- WS-in handle (shared with both client and out-republisher) ---
    let ws_handle = WsHandle::default();
    let ws_url = format!("ws://{}/ws", cfg.bridge.bind);

    // ---- WS-in client: forward local events → publisher ---------------
    let pub_for_events = publisher.clone();
    let on_event = build_event_callback(pub_for_events);
    let mut ws_cfg = WsConfig::new(ws_url.clone(), cfg.bridge.token.clone());
    ws_cfg.subscribe_to = cfg.ghost.export_kinds.clone();
    let ws_handle_in = ws_handle.clone();
    let ws_in_task = tokio::spawn(async move {
        ws_client::run_forever(ws_cfg, ws_handle_in, on_event).await;
    });

    // ---- WS-out task: republish remote events onto local bus ---------
    //
    // Important: we use the SAME WsHandle as the in-client (both clones
    // of the same Arc<Mutex<Option<...>>>). That way one WS connection
    // serves both directions — we subscribe AND publish on the same
    // socket. Reduces resource use and means we don't need a second
    // handshake.
    let ws_handle_out = ws_handle.clone();
    let qlog_out = qlog.clone();
    let log_every_n = cfg.ghost.log_every_n_frames;
    let ws_out_task = tokio::spawn(async move {
        let mut frame_count: u64 = 0;
        while let Some(ev) = inbound_rx.recv().await {
            match ws_handle_out.publish_custom(&ev.kind, ev.payload.clone()).await {
                Ok(true) => {}
                Ok(false) => {
                    warn!(kind = %ev.kind, "remote event dropped — no live WS");
                }
                Err(e) => {
                    warn!(kind = %ev.kind, "remote event publish failed: {e:#}");
                }
            }
            frame_count = frame_count.wrapping_add(1);
            if log_every_n > 0 && frame_count % log_every_n == 0 {
                let _ = qlog_out
                    .append_async(NewEntry::new(
                        EntryKind::Note,
                        MODULE,
                        serde_json::json!({
                            "event": "remote_republished_sampled",
                            "kind": ev.kind,
                            "from": ev.sender_id,
                            "count": frame_count,
                        }),
                    ))
                    .await;
            }
        }
        info!("ws-out task stopped (channel closed)");
    });

    // ---- Shutdown handling --------------------------------------------
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown requested — tearing down");

    qlog.append(NewEntry::new(
        EntryKind::Shutdown,
        MODULE,
        serde_json::json!({}),
    ))
    .ok();

    // Order: stop accepting work, then cancel tasks.
    discovery.shutdown();
    listener.shutdown();

    publisher_supervisor.abort();
    let _ = publisher_supervisor.await;
    ws_in_task.abort();
    let _ = ws_in_task.await;
    ws_out_task.abort();
    let _ = ws_out_task.await;
    listener_task.abort();
    let _ = listener_task.await;

    info!("ultron-ghost stopped");
    Ok(())
}

fn init_tracing() {
    let filter = std::env::var("ULTRON_LOG")
        .unwrap_or_else(|_| "info,ultron_ghost=info".into());
    let _ = tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .try_init();
}

// =====================================================================
// Event dispatch — WS-in events forward into the publisher
// =====================================================================

fn build_event_callback(publisher: Publisher) -> EventCallback {
    Arc::new(move |frame: serde_json::Value| {
        let publisher = publisher.clone();
        Box::pin(async move {
            let kind = frame
                .get("kind")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            if kind.is_empty() {
                return;
            }
            // Never forward our own remote-prefixed events back out —
            // would loop. (Belt-and-braces: we only subscribed to the
            // export allowlist anyway, and `ghost:*` kinds aren't on
            // it. But future config changes could trip this.)
            if kind.starts_with(ultron_types::GhostFrame::REMOTE_PREFIX) {
                return;
            }
            let payload = match frame.get("payload") {
                Some(p) => p.clone(),
                None => return,
            };
            if let Err(e) = publisher.publish_event(&kind, payload).await {
                warn!(kind = %kind, "publish_event failed: {e:#}");
            }
        })
    })
}
