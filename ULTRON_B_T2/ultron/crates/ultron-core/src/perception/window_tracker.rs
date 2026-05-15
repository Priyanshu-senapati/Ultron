//! # Active-Window Tracker
//!
//! Polls `GetForegroundWindow` on a configurable cadence. When the HWND
//! changes, looks up the title with `GetWindowTextW` and the owning
//! process executable with `OpenProcess` + `QueryFullProcessImageNameW`.
//!
//! ## Why polling and not a hook?
//!
//! `WinEventHook` (the event-driven path) would be lighter, but it requires
//! either an in-process message pump (we already have one for input hooks
//! and don't want a second) or out-of-process injection (which a single
//! daemon shouldn't need). A 500 ms poll is well under 0.05 % CPU on the
//! target machine and gives sub-second responsiveness to focus changes,
//! which is all downstream consumers need.
//!
//! ## Privacy
//!
//! We publish the **raw** title and process name on the local event bus —
//! the Insight Pulse and the HUD need them. The Ghost Network (Phase 1,
//! Module Q) hashes them before sending anywhere off-machine, and the
//! Privacy Router (Phase 4) governs anything else.
//!
//! On non-Windows targets this module is a no-op stub so the workspace
//! still builds for CI.

use crate::config::PerceptionConfig;
use crate::event_bus::EventBus;
use crate::perception::metrics::InputMetricsAggregator;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tracing::{debug, info, warn};
use ultron_quantum_log::{EntryKind, NewEntry, QuantumLog};
use ultron_types::{UltronEvent, WindowInfo};

#[derive(Clone)]
pub struct WindowTracker {
    cfg: PerceptionConfig,
    bus: EventBus,
    qlog: QuantumLog,
    metrics: InputMetricsAggregator,
    /// Fix 2 — the screenshotter is injected so a focus change can trigger
    /// a `ScreenshotReason::WindowChange` capture. Held as `Option` because
    /// some test paths construct a WindowTracker without one.
    screenshotter: Option<super::screenshot::Screenshotter>,
    stop: Arc<AtomicBool>,
}

impl WindowTracker {
    pub fn new(
        cfg: PerceptionConfig,
        bus: EventBus,
        qlog: QuantumLog,
        metrics: InputMetricsAggregator,
    ) -> Self {
        Self {
            cfg,
            bus,
            qlog,
            metrics,
            screenshotter: None,
            stop: Arc::new(AtomicBool::new(false)),
        }
    }

    /// Same as `new` but with a `Screenshotter` injected. Production code
    /// uses this; tests can stick with `new` to avoid spinning up GDI.
    pub fn with_screenshotter(
        cfg: PerceptionConfig,
        bus: EventBus,
        qlog: QuantumLog,
        metrics: InputMetricsAggregator,
        screenshotter: super::screenshot::Screenshotter,
    ) -> Self {
        Self {
            cfg,
            bus,
            qlog,
            metrics,
            screenshotter: Some(screenshotter),
            stop: Arc::new(AtomicBool::new(false)),
        }
    }

    /// Start the polling task. Returns the join handle so callers can abort.
    /// Idempotent in spirit — calling twice spawns two tasks, so don't.
    pub fn start(&self) -> tokio::task::JoinHandle<()> {
        let me = self.clone();
        tokio::spawn(async move {
            me.run().await;
        })
    }

    pub fn stop(&self) {
        self.stop.store(true, Ordering::SeqCst);
    }

    async fn run(self) {
        let interval_ms = self.cfg.window_poll_ms.max(100);
        let mut interval = tokio::time::interval(Duration::from_millis(interval_ms));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

        let mut last_hwnd: i64 = 0;
        let mut last_title: String = String::new();

        info!(poll_ms = interval_ms, "window tracker started");

        while !self.stop.load(Ordering::SeqCst) {
            interval.tick().await;

            let info = match tokio::task::spawn_blocking(current_foreground_window).await {
                Ok(Some(i)) => i,
                Ok(None) => continue,
                Err(e) => {
                    warn!("window probe panicked: {e}");
                    continue;
                }
            };

            // De-dupe by HWND first, then title (title can change on the same
            // HWND, e.g. a browser tab switch).
            if info.hwnd == last_hwnd && info.title == last_title {
                continue;
            }
            let hwnd_changed = info.hwnd != last_hwnd;
            last_hwnd = info.hwnd;
            last_title = info.title.clone();

            // Fix 4 — classify the foreground process. `None` if the user's
            // config doesn't have a mapping for this exe.
            let app_category = self.cfg.classify_app(&info.process_name);

            debug!(
                pid = info.pid,
                process = %info.process_name,
                category = ?app_category,
                title = %info.title,
                "foreground window changed"
            );

            self.metrics.feed_window_change(info.ts_unix_ms);
            self.bus.publish(UltronEvent::WindowChanged {
                title: info.title.clone(),
                process_name: info.process_name.clone(),
                pid: info.pid,
                hwnd: info.hwnd,
                app_category,
                ts_unix_ms: info.ts_unix_ms,
            });

            // Always log window changes — they're sparse and high-value.
            let mut payload = serde_json::to_value(&info).unwrap_or(serde_json::json!({}));
            if let Some(c) = app_category {
                if let Some(obj) = payload.as_object_mut() {
                    obj.insert(
                        "app_category".to_string(),
                        serde_json::Value::String(c.as_str().to_string()),
                    );
                }
            }
            if let Err(e) = self
                .qlog
                .append_async(NewEntry::new(
                    EntryKind::Event,
                    "perception/window",
                    payload,
                ))
                .await
            {
                warn!("qlog append failed in window tracker: {e}");
            }

            // Fix 2 — on a true HWND change (not just a title rename within
            // the same window), trigger a screenshot so Module O has visual
            // context for the new app. Title-only changes (e.g. switching
            // browser tabs) deliberately skip — too noisy.
            if hwnd_changed {
                if let Some(ss) = &self.screenshotter {
                    let ss = ss.clone();
                    tokio::spawn(async move {
                        if let Err(e) = ss
                            .capture_now(ultron_types::ScreenshotReason::WindowChange)
                            .await
                        {
                            debug!("window-change screenshot failed: {e:?}");
                        }
                    });
                }
            }
        }
        info!("window tracker stopped");
    }
}

// =====================================================================
// Windows implementation
// =====================================================================

#[cfg(windows)]
fn current_foreground_window() -> Option<WindowInfo> {
    use windows::Win32::Foundation::HWND;
    use windows::Win32::UI::WindowsAndMessaging::{
        GetForegroundWindow, GetWindowTextLengthW, GetWindowTextW, GetWindowThreadProcessId,
    };

    unsafe {
        let hwnd: HWND = GetForegroundWindow();
        if hwnd.0.is_null() {
            return None;
        }

        // Title.
        let len = GetWindowTextLengthW(hwnd);
        let title = if len > 0 {
            let mut buf = vec![0u16; (len as usize) + 1];
            let copied = GetWindowTextW(hwnd, &mut buf);
            if copied > 0 {
                String::from_utf16_lossy(&buf[..copied as usize])
            } else {
                String::new()
            }
        } else {
            String::new()
        };

        // PID + process name.
        let mut pid: u32 = 0;
        GetWindowThreadProcessId(hwnd, Some(&mut pid));
        let process_name = process_name_for_pid(pid).unwrap_or_default();

        Some(WindowInfo {
            title,
            process_name,
            pid,
            hwnd: hwnd.0 as i64,
            ts_unix_ms: chrono::Utc::now().timestamp_millis(),
        })
    }
}

#[cfg(windows)]
fn process_name_for_pid(pid: u32) -> Option<String> {
    use windows::core::PWSTR;
    use windows::Win32::Foundation::CloseHandle;
    use windows::Win32::System::Threading::{
        OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32,
        PROCESS_QUERY_LIMITED_INFORMATION,
    };

    if pid == 0 {
        return None;
    }
    unsafe {
        // PROCESS_QUERY_LIMITED_INFORMATION works on most processes including
        // protected ones, where PROCESS_QUERY_INFORMATION fails.
        let handle = match OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) {
            Ok(h) => h,
            Err(_) => return None,
        };

        let mut buf = vec![0u16; 1024];
        let mut size = buf.len() as u32;
        let ok = QueryFullProcessImageNameW(
            handle,
            PROCESS_NAME_WIN32,
            PWSTR(buf.as_mut_ptr()),
            &mut size,
        );
        let _ = CloseHandle(handle);

        if ok.is_err() || size == 0 {
            return None;
        }
        let path = String::from_utf16_lossy(&buf[..size as usize]);
        // Last component of the path = exe filename.
        let name = path
            .rsplit(|c| c == '\\' || c == '/')
            .next()
            .unwrap_or(&path)
            .to_string();
        Some(name)
    }
}

// =====================================================================
// Non-Windows stub — so workspace builds on Linux/macOS for CI/tests.
// =====================================================================

#[cfg(not(windows))]
fn current_foreground_window() -> Option<WindowInfo> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::PerceptionConfig;

    fn temp_qlog() -> QuantumLog {
        let mut p = std::env::temp_dir();
        p.push(format!("ultron_wt_test_{}.db", uuid::Uuid::new_v4()));
        QuantumLog::open(&p).unwrap()
    }

    #[test]
    fn constructs_and_stops() {
        let bus = EventBus::new(8);
        let qlog = temp_qlog();
        let metrics = InputMetricsAggregator::new(PerceptionConfig::default(), bus.clone(), qlog.clone());
        let wt = WindowTracker::new(PerceptionConfig::default(), bus, qlog, metrics);
        wt.stop();
        // We deliberately don't `start()` here — on non-Windows the probe is
        // None, and on CI we don't want a probe loop running. Just check the
        // type constructs cleanly.
    }

    #[cfg(not(windows))]
    #[test]
    fn stub_returns_none() {
        assert!(current_foreground_window().is_none());
    }
}
