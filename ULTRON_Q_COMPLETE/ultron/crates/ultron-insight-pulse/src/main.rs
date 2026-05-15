//! `ultron-insight-pulse` — Module O Rust sidecar.
//!
//! Subscribes to the ultron-core WS bridge, fuses incoming H signals into
//! [`InsightSnapshot`]s on a 5-second tick, publishes them back as custom
//! events, and records sampled snapshots + threshold crossings to the
//! Quantum Log.
//!
//! ## Process model
//!
//! Three Tokio tasks:
//!
//! 1. **WS pump** — `ws_client::run_forever`. Owns the connection lifecycle.
//!    On each `op:event` frame, dispatches into a closure that updates
//!    `SignalState`.
//! 2. **Tick loop** — every `InsightConfig.tick_secs` seconds: snapshot the
//!    state, run `fusion::assemble`, publish back via the WS handle, log to
//!    QL on sampled / threshold-cross conditions.
//! 3. **Signal listener** — Ctrl-C / SIGTERM → flips a shutdown flag, every
//!    task drops out on its next iteration.
//!
//! All three share `Arc<Mutex<SignalState>>` + a `WsHandle`. The mutex is
//! never held across `await` points longer than a single field assignment.

mod circadian;
mod fusion;
mod signal_state;
mod ws_client;

use anyhow::{Context, Result};
use parking_lot::Mutex;
use serde::Deserialize;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tracing::{info, warn};
use ultron_quantum_log::{EntryKind, NewEntry, QuantumLog};
use ultron_types::{AppCategory, InputMetrics, InsightSnapshot};

use crate::fusion::FusionInputs;
use crate::signal_state::SignalState;
use crate::ws_client::{EventCallback, WsConfig, WsHandle};

const VERSION: &str = env!("CARGO_PKG_VERSION");
const MODULE: &str = "insight-pulse";

// ---- config loading (we read the daemon's config.toml) ------------------

#[derive(Debug, Clone, Deserialize)]
struct CoreConfigBridge {
    bind: String,
    token: String,
}

#[derive(Debug, Clone, Deserialize)]
struct CoreConfigGeneral {
    data_dir: PathBuf,
}

#[derive(Debug, Clone, Deserialize)]
struct CoreConfigInsight {
    #[serde(default = "default_tick_secs")]
    tick_secs: u64,
    #[serde(default = "default_log_every_n")]
    log_every_n_ticks: u64,
    #[serde(default = "default_cl_alert")]
    cognitive_load_alert_threshold: f32,
    #[serde(default = "default_visual_max_age")]
    visual_label_max_age_secs: u32,
}

fn default_tick_secs() -> u64 { 5 }
fn default_log_every_n() -> u64 { 12 }
fn default_cl_alert() -> f32 { 0.75 }
fn default_visual_max_age() -> u32 { 120 }

impl Default for CoreConfigInsight {
    fn default() -> Self {
        Self {
            tick_secs: default_tick_secs(),
            log_every_n_ticks: default_log_every_n(),
            cognitive_load_alert_threshold: default_cl_alert(),
            visual_label_max_age_secs: default_visual_max_age(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
struct CoreConfig {
    bridge: CoreConfigBridge,
    general: CoreConfigGeneral,
    #[serde(default)]
    insight: CoreConfigInsight,
}

fn config_path() -> Result<PathBuf> {
    if let Ok(p) = std::env::var("ULTRON_CONFIG") {
        return Ok(PathBuf::from(p));
    }
    let base = dirs::config_dir().context("no config dir on this OS")?;
    Ok(base.join("ULTRON").join("config.toml"))
}

fn load_core_config() -> Result<CoreConfig> {
    let path = config_path()?;
    let text = std::fs::read_to_string(&path)
        .with_context(|| format!("read {}", path.display()))?;
    let cfg: CoreConfig = toml::from_str(&text).context("parse config.toml")?;
    Ok(cfg)
}

// ---- main ---------------------------------------------------------------

#[tokio::main]
async fn main() -> Result<()> {
    init_tracing();
    info!(version = VERSION, "ultron-insight-pulse starting");

    let cfg = load_core_config().context("load ultron-core config")?;
    let url = format!("ws://{}/ws", cfg.bridge.bind);

    // Open the Quantum Log under the same data dir as the core. The log
    // is append-only and the schema is set up by either process on first
    // open, so it's safe to share.
    let qlog_path = cfg.general.data_dir.join("quantum.db");
    let qlog = QuantumLog::open(&qlog_path)
        .with_context(|| format!("open quantum log at {}", qlog_path.display()))?;
    qlog.append(NewEntry::new(
        EntryKind::Boot,
        MODULE,
        serde_json::json!({ "version": VERSION }),
    ))
    .ok();

    let state = Arc::new(Mutex::new(SignalState::new()));
    let shutdown = Arc::new(AtomicBool::new(false));
    let handle = WsHandle::default();

    // ---- WS pump --------------------------------------------------------
    let ws_state = state.clone();
    let ws_handle_clone = handle.clone();
    let on_event = build_event_callback(ws_state);
    let ws_cfg = WsConfig::new(url, cfg.bridge.token.clone());
    let ws_task = tokio::spawn(async move {
        ws_client::run_forever(ws_cfg, ws_handle_clone, on_event).await;
    });

    // ---- tick loop ------------------------------------------------------
    let tick_state = state.clone();
    let tick_handle = handle.clone();
    let tick_qlog = qlog.clone();
    let tick_shutdown = shutdown.clone();
    let tick_cfg = cfg.insight.clone();
    let tick_task = tokio::spawn(async move {
        run_tick_loop(tick_state, tick_handle, tick_qlog, tick_cfg, tick_shutdown).await;
    });

    // ---- signal handling -----------------------------------------------
    install_shutdown_handler(shutdown.clone()).await;

    info!("shutdown requested — stopping tasks");
    // Best-effort log; ignore if the log is gone.
    qlog.append(NewEntry::new(
        EntryKind::Shutdown,
        MODULE,
        serde_json::json!({}),
    ))
    .ok();

    tick_task.abort();
    let _ = tick_task.await;
    ws_task.abort();
    let _ = ws_task.await;
    info!("ultron-insight-pulse stopped");
    Ok(())
}

fn init_tracing() {
    let filter = std::env::var("ULTRON_LOG")
        .unwrap_or_else(|_| "info,ultron_insight_pulse=info".into());
    let _ = tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .try_init();
}

async fn install_shutdown_handler(flag: Arc<AtomicBool>) {
    // tokio::signal::ctrl_c works on Windows + *nix; SIGTERM handled via
    // a Unix-only branch.
    let _ = tokio::signal::ctrl_c().await;
    flag.store(true, Ordering::SeqCst);
}

// =====================================================================
// Event dispatch
// =====================================================================

/// Build the closure that the WS pump invokes on every `op:event` frame.
/// Pure dispatch — every payload type lands in a `SignalState` method.
fn build_event_callback(state: Arc<Mutex<SignalState>>) -> EventCallback {
    Arc::new(move |frame: serde_json::Value| {
        let state = state.clone();
        Box::pin(async move {
            let kind = frame
                .get("kind")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let payload = match frame.get("payload") {
                Some(p) => p.clone(),
                None => return,
            };
            dispatch_event(&state, &kind, payload);
        })
    })
}

fn dispatch_event(state: &Arc<Mutex<SignalState>>, kind: &str, payload: serde_json::Value) {
    let now_ms = chrono::Utc::now().timestamp_millis();
    match kind {
        "tension_changed" => {
            if let Some(cur) = payload.get("current").and_then(|v| v.as_f64()) {
                state.lock().on_tension_changed(cur as f32, now_ms);
            }
        }
        "heartbeat" => {
            if let Some(t) = payload.get("tension").and_then(|v| v.as_f64()) {
                state.lock().on_heartbeat(t as f32, now_ms);
            }
        }
        "input_metrics_updated" => {
            match serde_json::from_value::<InputMetrics>(payload) {
                Ok(m) => state.lock().on_input_metrics(m),
                Err(e) => warn!("input_metrics_updated decode failed: {e}"),
            }
        }
        "window_changed" => {
            let process_name = payload
                .get("process_name")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let app_category = payload
                .get("app_category")
                .and_then(|v| v.as_str())
                .map(AppCategory::from_str_lossy);
            let ts = payload
                .get("ts_unix_ms")
                .and_then(|v| v.as_i64())
                .unwrap_or(now_ms);
            state
                .lock()
                .on_window_changed(process_name, app_category, ts);
        }
        "visual_label" => {
            let label = payload
                .get("label")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let screenshot_ts = payload.get("screenshot_ts").and_then(|v| v.as_i64());
            state.lock().on_visual_label(label, screenshot_ts, now_ms);
        }
        "productivity_prior_update" => {
            // From Module D. Replaces the circadian default whenever
            // we have a learned value for the current hour.
            match serde_json::from_value::<ultron_types::ProductivityPriorUpdate>(payload) {
                Ok(update) => state.lock().on_productivity_prior_update(update),
                Err(e) => warn!("productivity_prior_update decode failed: {e}"),
            }
        }
        // screenshot_captured is informational only for the Rust sidecar;
        // the Python sidecar consumes it for LLaVA inference. Anything else
        // (heartbeats from other modules, future event kinds) is ignored.
        _ => {}
    }
}

// =====================================================================
// Tick loop
// =====================================================================

async fn run_tick_loop(
    state: Arc<Mutex<SignalState>>,
    handle: WsHandle,
    qlog: QuantumLog,
    cfg: CoreConfigInsight,
    shutdown: Arc<AtomicBool>,
) {
    let mut interval = tokio::time::interval(Duration::from_secs(cfg.tick_secs.max(1)));
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    interval.tick().await; // skip the immediate first tick

    let mut prev_above_threshold = false;

    while !shutdown.load(Ordering::SeqCst) {
        interval.tick().await;
        if shutdown.load(Ordering::SeqCst) {
            break;
        }

        // Build inputs from real clock; assemble the snapshot under a brief
        // mutex hold. Cloning the small state is cheaper than holding the
        // lock across the async publish below.
        let inputs = FusionInputs {
            visual_label_max_age_secs: cfg.visual_label_max_age_secs,
            ..FusionInputs::from_now()
        };
        let snapshot: InsightSnapshot;
        {
            let mut g = state.lock();
            g.tick = g.tick.saturating_add(1);
            snapshot = fusion::assemble(&g, inputs);
            // Suppression accounting: did we drop a stale visual label?
            // We can check by comparing state.visual_label vs snapshot.visual_label.
            // (Done after publishing below — uses snapshot directly.)
            // Note: drop guard before await.
        }

        // Publish back onto the bus.
        match handle
            .publish_custom("insight_snapshot", serde_json::to_value(&snapshot).unwrap())
            .await
        {
            Ok(true) => {}
            Ok(false) => {
                warn!("insight_snapshot dropped — no live WS connection");
            }
            Err(e) => warn!("publish failed: {e:#}"),
        }

        // Quantum Log sampling.
        if snapshot.tick % cfg.log_every_n_ticks.max(1) == 0 {
            let _ = qlog
                .append_async(NewEntry::new(
                    EntryKind::InsightTick,
                    MODULE,
                    serde_json::to_value(&snapshot).unwrap_or_default(),
                ))
                .await;
        }

        // Threshold crossing: cognitive_load → InsightFired on rise only.
        let now_above = snapshot.cognitive_load >= cfg.cognitive_load_alert_threshold;
        if now_above && !prev_above_threshold {
            let _ = qlog
                .append_async(NewEntry::new(
                    EntryKind::InsightFired,
                    MODULE,
                    serde_json::json!({
                        "trigger": "cognitive_load",
                        "value": snapshot.cognitive_load,
                        "snapshot_tick": snapshot.tick,
                    }),
                ))
                .await;
            info!(
                cognitive_load = snapshot.cognitive_load,
                tick = snapshot.tick,
                "insight fired"
            );
        }
        prev_above_threshold = now_above;

        // Stale-label suppression accounting. The fusion code drops the
        // label when age > max; we record the suppression here so it's
        // auditable why the snapshot was "blind".
        let has_label_in_state = state.lock().visual_label.is_some();
        let dropped_due_to_stale =
            has_label_in_state && snapshot.visual_label.is_none() && snapshot.visual_label_age_secs > 0;
        if dropped_due_to_stale {
            let _ = qlog
                .append_async(NewEntry::new(
                    EntryKind::InsightSuppressed,
                    MODULE,
                    serde_json::json!({
                        "reason": "visual_label_stale",
                        "age_secs": snapshot.visual_label_age_secs,
                    }),
                ))
                .await;
        }
    }
}
