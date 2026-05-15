//! ULTRON v5.1 — Module A: ultron-core daemon entry point.
//!
//! Modes:
//!   ultron-core               run as a foreground console daemon (dev)
//!   ultron-core --service     run as the Windows Service (SCM-invoked)
//!   ultron-core --install     register the service (elevated)
//!   ultron-core --uninstall   remove the service (elevated)
//!   ultron-core --verify      walk the Quantum Log and verify hash chain
//!   ultron-core --print-token print the bridge token (for client setup)

use anyhow::Context;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::oneshot;
use tracing::{error, info, warn};
use tracing_subscriber::{fmt, prelude::*, EnvFilter};
use ultron_quantum_log::{EntryKind, NewEntry, QuantumLog};
use ultron_types::UltronEvent;

mod config;
mod error;
mod event_bus;
mod input_monitor;
mod perception;
mod tension;
mod ws_server;

#[cfg(windows)]
mod service;

use crate::config::Config;
use crate::event_bus::EventBus;
use crate::input_monitor::InputMonitor;
use crate::perception::{InputMetricsAggregator, Screenshotter, WindowTracker};
use crate::tension::TensionTracker;
use crate::ws_server::WsState;

#[derive(Debug, Clone, Copy)]
enum Mode {
    Console,
    Service,
    Install,
    Uninstall,
    Verify,
    PrintToken,
}

fn parse_mode() -> Mode {
    match std::env::args().nth(1).as_deref() {
        Some("--service") => Mode::Service,
        Some("--install") => Mode::Install,
        Some("--uninstall") => Mode::Uninstall,
        Some("--verify") => Mode::Verify,
        Some("--print-token") => Mode::PrintToken,
        _ => Mode::Console,
    }
}

fn main() -> anyhow::Result<()> {
    let mode = parse_mode();
    match mode {
        Mode::Install => {
            install_tracing(false)?;
            #[cfg(windows)]
            {
                service::install().context("install service")?;
            }
            #[cfg(not(windows))]
            {
                eprintln!("--install is only supported on Windows.");
                std::process::exit(2);
            }
            Ok(())
        }
        Mode::Uninstall => {
            install_tracing(false)?;
            #[cfg(windows)]
            {
                service::uninstall().context("uninstall service")?;
            }
            #[cfg(not(windows))]
            {
                eprintln!("--uninstall is only supported on Windows.");
                std::process::exit(2);
            }
            Ok(())
        }
        Mode::Verify => {
            install_tracing(false)?;
            let cfg = Config::load_or_create().context("load config")?;
            let q = QuantumLog::open(cfg.quantum_log_path()).context("open quantum log")?;
            let n = q.verify_chain().context("verify chain")?;
            println!(
                "✓ quantum log OK: {n} entries verified, db at {}",
                q.path().display()
            );
            Ok(())
        }
        Mode::PrintToken => {
            // Don't install full tracing; we want clean stdout.
            let cfg = Config::load_or_create().context("load config")?;
            println!("{}", cfg.bridge.token);
            Ok(())
        }
        Mode::Service => {
            // Tracing must go to a file when running as a service.
            install_tracing(true)?;
            #[cfg(windows)]
            {
                service::run_as_service().context("run as service")?;
            }
            #[cfg(not(windows))]
            {
                eprintln!("--service mode is only supported on Windows.");
                std::process::exit(2);
            }
            Ok(())
        }
        Mode::Console => {
            install_tracing(false)?;
            let rt = tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()?;
            let (tx, rx) = oneshot::channel::<()>();

            // Wire Ctrl-C to shutdown.
            let tx_arc = Arc::new(parking_lot::Mutex::new(Some(tx)));
            ctrlc_handler(tx_arc);

            rt.block_on(async move {
                if let Err(e) = run_daemon(rx).await {
                    error!("daemon error: {e:?}");
                    std::process::exit(1);
                }
            });
            Ok(())
        }
    }
}

/// Install tracing. When `to_file` is true, we write JSON-line logs into the
/// configured logs dir (`%APPDATA%/ULTRON/logs`). Otherwise, pretty stdout.
fn install_tracing(to_file: bool) -> anyhow::Result<()> {
    let env_filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("info,ultron_core=debug,ultron_quantum_log=info"));

    if to_file {
        let cfg = Config::load_or_create()?;
        std::fs::create_dir_all(&cfg.general.logs_dir)?;
        let appender = tracing_appender::rolling::daily(&cfg.general.logs_dir, "ultron-core.log");
        // Leak the guard — process is long-lived.
        let (nb, guard) = tracing_appender::non_blocking(appender);
        Box::leak(Box::new(guard));
        tracing_subscriber::registry()
            .with(env_filter)
            .with(fmt::layer().json().with_writer(nb).with_ansi(false))
            .init();
    } else {
        tracing_subscriber::registry()
            .with(env_filter)
            .with(fmt::layer().pretty())
            .init();
    }
    Ok(())
}

/// Ctrl-C handler. Sends shutdown once, ignores subsequent presses.
fn ctrlc_handler(tx: Arc<parking_lot::Mutex<Option<oneshot::Sender<()>>>>) {
    // Don't pull in the ctrlc crate — std works for our needs.
    std::thread::spawn(move || {
        // tokio::signal needs a runtime, so use a tiny one just for the signal.
        let rt = match tokio::runtime::Builder::new_current_thread().enable_all().build() {
            Ok(r) => r,
            Err(_) => return,
        };
        rt.block_on(async move {
            if let Err(e) = tokio::signal::ctrl_c().await {
                warn!("ctrl_c handler failed: {e:?}");
                return;
            }
            info!("Ctrl-C received, shutting down");
            if let Some(s) = tx.lock().take() {
                let _ = s.send(());
            }
        });
    });
}

/// The actual daemon loop. Called from both console and service modes.
pub async fn run_daemon(shutdown_rx: oneshot::Receiver<()>) -> anyhow::Result<()> {
    let started_at = std::time::Instant::now();
    let cfg = Arc::new(Config::load_or_create()?);
    info!(
        version = env!("CARGO_PKG_VERSION"),
        data_dir = %cfg.general.data_dir.display(),
        bind = %cfg.bridge.bind,
        "ULTRON core booting"
    );

    // ---------------------------------------------------------------------
    // Quantum Log first — every subsystem boot logs through it.
    // ---------------------------------------------------------------------
    let qlog = QuantumLog::open(cfg.quantum_log_path()).context("open quantum log")?;
    info!(path = %qlog.path().display(), "quantum log open");

    qlog.append(NewEntry::new(
        EntryKind::Boot,
        "ultron-core",
        serde_json::json!({
            "version": env!("CARGO_PKG_VERSION"),
            "user": cfg.general.user_name,
            "pid": std::process::id(),
            "data_dir": cfg.general.data_dir,
            "bridge_bind": cfg.bridge.bind,
            "heartbeat_secs": cfg.general.heartbeat_secs,
            "platform": std::env::consts::OS,
            "arch": std::env::consts::ARCH,
        }),
    ))?;

    // ---------------------------------------------------------------------
    // Event bus + tension tracker
    // ---------------------------------------------------------------------
    let bus = EventBus::new(2048);
    let tracker = TensionTracker::new(cfg.tension.clone(), bus.clone());
    let _tension_ticker = tracker.clone().spawn_ticker();

    // ---------------------------------------------------------------------
    // Perception (Phase 1, Module H): metrics aggregator, window tracker,
    // and screenshotter. All publish onto the same event bus.
    //
    // Build order: metrics → screenshotter → window_tracker (needs the
    // screenshotter for Fix 2's WindowChange capture).
    // ---------------------------------------------------------------------
    let metrics = InputMetricsAggregator::new(
        cfg.perception.clone(),
        bus.clone(),
        qlog.clone(),
    );
    let metrics_handle = metrics.clone().spawn_ticker();

    let screenshotter = Screenshotter::new(
        cfg.perception.clone(),
        bus.clone(),
        qlog.clone(),
        &cfg.general.data_dir,
    );
    let screenshot_periodic_handle = screenshotter.start_periodic();
    // Fix 3 — listen for `request_screenshot` custom events on the bus.
    let screenshot_listener_handle = screenshotter.start_listener();

    let window_tracker = WindowTracker::with_screenshotter(
        cfg.perception.clone(),
        bus.clone(),
        qlog.clone(),
        metrics.clone(),
        screenshotter.clone(),
    );
    let window_handle = window_tracker.start();

    // ---------------------------------------------------------------------
    // Input monitor (WinAPI hooks on Windows; no-op stub elsewhere)
    // ---------------------------------------------------------------------
    let monitor = InputMonitor::new(
        cfg.input.clone(),
        bus.clone(),
        tracker.clone(),
        metrics.clone(),
        qlog.clone(),
    );
    if let Err(e) = monitor.start() {
        warn!("input monitor failed to start: {e:?} (continuing without it)");
        let _ = qlog.append(NewEntry::new(
            EntryKind::Error,
            "input_monitor",
            serde_json::json!({ "error": format!("{e:?}") }),
        ));
    }

    // ---------------------------------------------------------------------
    // WS bridge
    // ---------------------------------------------------------------------
    let (ws_shutdown_tx, ws_shutdown_rx) = oneshot::channel::<()>();
    let ws_state = WsState {
        cfg: cfg.clone(),
        bus: bus.clone(),
        qlog: qlog.clone(),
    };
    let ws_handle = tokio::spawn(async move {
        if let Err(e) = ws_server::run(ws_state, ws_shutdown_rx).await {
            error!("ws server error: {e:?}");
        }
    });

    // ---------------------------------------------------------------------
    // Heartbeat
    // ---------------------------------------------------------------------
    let bus_hb = bus.clone();
    let qlog_hb = qlog.clone();
    let tracker_hb = tracker.clone();
    let hb_secs = cfg.general.heartbeat_secs.max(1);
    let started_for_hb = started_at;
    let heartbeat = tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(hb_secs));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let mut tick: u64 = 0;
        loop {
            interval.tick().await;
            tick += 1;
            let snap = tracker_hb.snapshot();
            let uptime = started_for_hb.elapsed().as_secs();
            bus_hb.publish(UltronEvent::Heartbeat {
                tension: snap.value,
                uptime_secs: uptime,
            });
            // Log heartbeat snapshot every 12th tick (≈1/min if hb=5s) to keep
            // the log readable. Adjust as needed.
            if tick % 12 == 0 {
                let _ = qlog_hb
                    .append_async(NewEntry::new(
                        EntryKind::HeartbeatSnapshot,
                        "ultron-core",
                        serde_json::json!({
                            "uptime_secs": uptime,
                            "tension": snap,
                        }),
                    ))
                    .await;
            }
        }
    });

    // Announce ready.
    bus.publish(UltronEvent::ServiceState {
        state: "running".into(),
    });
    info!(
        "ULTRON core ready — bridge: ws://{}/ws — token in --print-token",
        cfg.bridge.bind
    );

    // ---------------------------------------------------------------------
    // Wait for shutdown
    // ---------------------------------------------------------------------
    let _ = shutdown_rx.await;
    info!("daemon shutdown requested");

    // Fix 8 — collect and explicitly abort every task we spawned, then
    // await each so the process doesn't race the runtime down. Drop order
    // mirrors the build order in reverse.
    //
    // First, flip the cooperative-shutdown flags so loops exit on their
    // next iteration. Then abort + await every JoinHandle.
    monitor.stop();
    window_tracker.stop();
    screenshotter.stop();

    let _ = ws_shutdown_tx.send(());
    heartbeat.abort();
    let _ = heartbeat.await;

    // Perception handles.
    window_handle.abort();
    let _ = window_handle.await;
    screenshot_listener_handle.abort();
    let _ = screenshot_listener_handle.await;
    if let Some(h) = screenshot_periodic_handle {
        h.abort();
        let _ = h.await;
    }
    metrics_handle.abort();
    let _ = metrics_handle.await;

    // WS server (graceful — uses its own shutdown channel above).
    let _ = ws_handle.await;

    let total_uptime = started_at.elapsed().as_secs();
    qlog.append(NewEntry::new(
        EntryKind::Shutdown,
        "ultron-core",
        serde_json::json!({
            "uptime_secs": total_uptime,
            "tension_at_exit": tracker.snapshot(),
        }),
    ))?;
    info!(uptime_secs = total_uptime, "ULTRON core stopped cleanly");
    Ok(())
}
