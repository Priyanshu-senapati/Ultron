//! `ultron-memory-engine` — Module D Rust sidecar.
//!
//! Subscribes to the ultron-core WS bridge, persists every
//! `insight_snapshot` and `visual_label` event to a private SQLite DB,
//! and (from Turn 2 onward) publishes learned `productivity_prior_update`
//! events back onto the bus on a configurable cadence.
//!
//! ## Process model
//!
//! Two long-lived Tokio tasks:
//!
//! 1. **WS pump** — `ws_client::run_forever`. Owns the connection,
//!    invokes a handler closure for each `op:event` frame.
//! 2. **Prior tick** — every `tick_secs` seconds: compute the prior via
//!    [`learning::ProductivityLearner::compute`] and publish back via
//!    the WS handle.
//!
//! A Quantum Log entry is emitted on boot, shutdown, and on each prior
//! publish (sampled — every Nth tick — to keep volume manageable).

mod learning;
mod patterns;
mod store;
mod ws_client;

use anyhow::{Context, Result};
use serde::Deserialize;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tracing::{info, warn};
use ultron_quantum_log::{EntryKind, NewEntry, QuantumLog};
use ultron_types::{InsightSnapshot, ProductivityPriorUpdate};

use crate::learning::ProductivityLearner;
use crate::store::MemoryStore;
use crate::ws_client::{EventCallback, WsConfig, WsHandle};

const VERSION: &str = env!("CARGO_PKG_VERSION");
const MODULE: &str = "memory-engine";

// ---- config loading -----------------------------------------------------

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
struct CoreConfigMemory {
    #[serde(default = "default_prior_tick_secs")]
    prior_tick_secs: u64,
    #[serde(default = "default_log_every_n")]
    log_every_n_ticks: u64,
    /// How often the pattern detectors run, in seconds. Patterns
    /// change slowly (days of history before signal is meaningful), so
    /// a much slower cadence than the prior tick is right. Default
    /// 3600 (once per hour).
    #[serde(default = "default_pattern_tick_secs")]
    pattern_tick_secs: u64,
    /// Override path for the memory DB. Empty string or absent ⇒ falls
    /// back to `<data_dir>/memory.db`. We accept a `String` here (rather
    /// than `Option<PathBuf>`) to mirror exactly how the core writes the
    /// field out — TOML round-trips empty-string defaults cleanly.
    #[serde(default)]
    db_path: String,
}

fn default_prior_tick_secs() -> u64 {
    // Build prompt says "every 10–15 minutes". Default to 10.
    10 * 60
}
fn default_log_every_n() -> u64 {
    // With a 10-minute prior tick, logging every 6th = once per hour.
    6
}
fn default_pattern_tick_secs() -> u64 {
    // Patterns need real history to be meaningful — hourly is plenty.
    60 * 60
}

impl Default for CoreConfigMemory {
    fn default() -> Self {
        Self {
            prior_tick_secs: default_prior_tick_secs(),
            log_every_n_ticks: default_log_every_n(),
            pattern_tick_secs: default_pattern_tick_secs(),
            db_path: String::new(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
struct CoreConfig {
    bridge: CoreConfigBridge,
    general: CoreConfigGeneral,
    #[serde(default)]
    memory: CoreConfigMemory,
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
    info!(version = VERSION, "ultron-memory-engine starting");

    let cfg = load_core_config().context("load ultron-core config")?;
    let url = format!("ws://{}/ws", cfg.bridge.bind);

    // Open the Quantum Log for boot/shutdown bookends.
    let qlog_path = cfg.general.data_dir.join("quantum.db");
    let qlog = QuantumLog::open(&qlog_path)
        .with_context(|| format!("open quantum log at {}", qlog_path.display()))?;
    qlog.append(NewEntry::new(
        EntryKind::Boot,
        MODULE,
        serde_json::json!({ "version": VERSION }),
    ))
    .ok();

    // Open the memory database — D's private store.
    let db_path = if cfg.memory.db_path.trim().is_empty() {
        cfg.general.data_dir.join("memory.db")
    } else {
        PathBuf::from(&cfg.memory.db_path)
    };
    let store = MemoryStore::open(&db_path)
        .with_context(|| format!("open memory db at {}", db_path.display()))?;
    info!(
        path = %store.path().display(),
        existing = store.snapshot_count().unwrap_or(0),
        "memory store ready"
    );

    let learner = ProductivityLearner::new(store.clone());
    let shutdown = Arc::new(AtomicBool::new(false));
    let handle = WsHandle::default();

    // ---- WS pump --------------------------------------------------------
    let on_event = build_event_callback(store.clone());
    let ws_cfg = WsConfig::new(url, cfg.bridge.token.clone());
    let ws_handle_clone = handle.clone();
    let ws_task = tokio::spawn(async move {
        ws_client::run_forever(ws_cfg, ws_handle_clone, on_event).await;
    });

    // ---- Prior-tick loop -----------------------------------------------
    let tick_handle = handle.clone();
    let tick_qlog = qlog.clone();
    let tick_shutdown = shutdown.clone();
    let tick_cfg = cfg.memory.clone();
    let tick_task = tokio::spawn(async move {
        run_prior_loop(learner, tick_handle, tick_qlog, tick_cfg, tick_shutdown).await;
    });

    // ---- Pattern-detection loop (Turn 3) -------------------------------
    // Runs on a much slower cadence than the prior tick — patterns need
    // days of history before they're meaningful, and publishing them
    // every five seconds would just be noise. Default cadence: 1h.
    let detector = patterns::PatternDetector::new(store.clone());
    let pattern_handle = handle.clone();
    let pattern_qlog = qlog.clone();
    let pattern_shutdown = shutdown.clone();
    let pattern_cfg = cfg.memory.clone();
    let pattern_task = tokio::spawn(async move {
        run_pattern_loop(detector, pattern_handle, pattern_qlog, pattern_cfg, pattern_shutdown).await;
    });

    // ---- Signal handling -----------------------------------------------
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown requested — stopping tasks");
    shutdown.store(true, Ordering::SeqCst);
    qlog.append(NewEntry::new(
        EntryKind::Shutdown,
        MODULE,
        serde_json::json!({}),
    ))
    .ok();

    pattern_task.abort();
    let _ = pattern_task.await;
    tick_task.abort();
    let _ = tick_task.await;
    ws_task.abort();
    let _ = ws_task.await;
    info!("ultron-memory-engine stopped");
    Ok(())
}

fn init_tracing() {
    let filter = std::env::var("ULTRON_LOG")
        .unwrap_or_else(|_| "info,ultron_memory_engine=info".into());
    let _ = tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .try_init();
}

// =====================================================================
// Event dispatch
// =====================================================================

fn build_event_callback(store: MemoryStore) -> EventCallback {
    Arc::new(move |frame: serde_json::Value| {
        let store = store.clone();
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
            // Heavy work (SQLite write) goes on a blocking thread so we
            // don't stall the WS pump if the disk is slow.
            tokio::task::spawn_blocking(move || dispatch_event(&store, &kind, payload))
                .await
                .ok();
        })
    })
}

fn dispatch_event(store: &MemoryStore, kind: &str, payload: serde_json::Value) {
    let now_ms = chrono::Utc::now().timestamp_millis();
    match kind {
        "insight_snapshot" => match serde_json::from_value::<InsightSnapshot>(payload) {
            Ok(snap) => {
                if let Err(e) = store.insert_snapshot(&snap) {
                    warn!("snapshot insert failed: {e:#}");
                }
            }
            Err(e) => warn!("insight_snapshot decode failed: {e}"),
        },
        "visual_label" => {
            let label = payload
                .get("label")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_string();
            if label.is_empty() {
                return;
            }
            let screenshot_ts = payload.get("screenshot_ts").and_then(|v| v.as_i64());
            if let Err(e) = store.insert_visual_label(&label, screenshot_ts, now_ms) {
                warn!("visual_label insert failed: {e:#}");
            }
        }
        _ => {}
    }
}

// =====================================================================
// Prior-tick loop
// =====================================================================

async fn run_prior_loop(
    learner: ProductivityLearner,
    handle: WsHandle,
    qlog: QuantumLog,
    cfg: CoreConfigMemory,
    shutdown: Arc<AtomicBool>,
) {
    let mut interval = tokio::time::interval(Duration::from_secs(cfg.prior_tick_secs.max(60)));
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    interval.tick().await; // skip immediate first tick

    let mut tick_count: u64 = 0;

    while !shutdown.load(Ordering::SeqCst) {
        interval.tick().await;
        if shutdown.load(Ordering::SeqCst) {
            break;
        }

        let now_ms = chrono::Utc::now().timestamp_millis();

        // Compute via the learner. Wrap in spawn_blocking because Turn 2's
        // implementation will do SQLite reads.
        let learner = learner.clone();
        let update = match tokio::task::spawn_blocking(move || learner.compute(now_ms)).await {
            Ok(Ok(u)) => u,
            Ok(Err(e)) => {
                warn!("productivity compute failed: {e:#}");
                continue;
            }
            Err(e) => {
                warn!("compute task panicked: {e}");
                continue;
            }
        };

        // Publish back onto the bus.
        let payload = match serde_json::to_value(&update) {
            Ok(v) => v,
            Err(e) => {
                warn!("serialise prior update failed: {e}");
                continue;
            }
        };
        match handle
            .publish_custom(ProductivityPriorUpdate::KIND, payload.clone())
            .await
        {
            Ok(true) => {}
            Ok(false) => warn!("prior update dropped — no live WS connection"),
            Err(e) => warn!("publish failed: {e:#}"),
        }

        tick_count = tick_count.wrapping_add(1);
        if tick_count % cfg.log_every_n_ticks.max(1) == 0 {
            let _ = qlog
                .append_async(NewEntry::new(
                    EntryKind::Note,
                    MODULE,
                    serde_json::json!({
                        "event": "prior_published",
                        "has_data": update.has_any_data(),
                        "schema_version": update.schema_version,
                    }),
                ))
                .await;
        }
    }
}

// =====================================================================
// Pattern-tick loop (Turn 3)
// =====================================================================

async fn run_pattern_loop(
    detector: patterns::PatternDetector,
    handle: WsHandle,
    qlog: QuantumLog,
    cfg: CoreConfigMemory,
    shutdown: Arc<AtomicBool>,
) {
    // Patterns are slow signals — never tick faster than once a minute.
    let mut interval =
        tokio::time::interval(Duration::from_secs(cfg.pattern_tick_secs.max(60)));
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    interval.tick().await; // skip immediate first tick

    while !shutdown.load(Ordering::SeqCst) {
        interval.tick().await;
        if shutdown.load(Ordering::SeqCst) {
            break;
        }

        let now_ms = chrono::Utc::now().timestamp_millis();

        // Detection does SQLite reads + writes — must run on a blocking
        // thread so we don't stall the WS pump if the disk is slow.
        let det = detector.clone();
        let update = match tokio::task::spawn_blocking(move || det.detect(now_ms)).await {
            Ok(Ok(u)) => u,
            Ok(Err(e)) => {
                warn!("pattern detect failed: {e:#}");
                continue;
            }
            Err(e) => {
                warn!("pattern detect task panicked: {e}");
                continue;
            }
        };

        // Publish back onto the bus.
        let payload = match serde_json::to_value(&update) {
            Ok(v) => v,
            Err(e) => {
                warn!("serialise patterns update failed: {e}");
                continue;
            }
        };
        match handle
            .publish_custom(ultron_types::PatternsUpdate::KIND, payload)
            .await
        {
            Ok(true) => {}
            Ok(false) => warn!("patterns update dropped — no live WS connection"),
            Err(e) => warn!("publish failed: {e:#}"),
        }

        // Always log pattern cycles — they're rare and high-value.
        let _ = qlog
            .append_async(NewEntry::new(
                EntryKind::Note,
                MODULE,
                serde_json::json!({
                    "event": "patterns_published",
                    "count": update.patterns.len(),
                    "schema_version": update.schema_version,
                }),
            ))
            .await;
    }
}
