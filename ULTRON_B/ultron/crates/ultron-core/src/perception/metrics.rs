//! # Input Metrics Aggregator
//!
//! Maintains sliding-window deques of recent input signals and computes
//! human-meaningful metrics on a periodic tick.
//!
//! ## Why this is separate from `tension::TensionTracker`
//!
//! The tension tracker emits a single composite **score** that drives
//! behaviour gating. It deliberately compresses everything to one number.
//! The metrics aggregator emits the **components** so other modules
//! (Insight Pulse, the HUD, the Ghost Network sync) can reason about
//! *why* tension is what it is.
//!
//! Both subscribe to the same raw signal stream, but they own independent
//! state, independent thresholds, and independent ticks.
//!
//! ## Computation, briefly
//!
//! - **WPM**: count letter / digit / symbol / whitespace key-down events in
//!   the rolling window, divide by 5 (avg word length), scale to per-minute.
//! - **Backspace rate**: count of `Backspace` key-down events per minute.
//! - **Backspace storm**: `>= storm_threshold` backspaces inside the last
//!   `storm_window_ms`. Boolean. Hysteresis is implicit in the window.
//! - **Typing rhythm variance**: coefficient of variation of inter-keystroke
//!   intervals. `0.0` = metronomic, saturates at `cv = 1.5`.
//! - **Mouse velocity**: total Euclidean distance covered / window duration.
//! - **Mouse hesitation**: per-minute count of dx/dy sign reversals,
//!   normalised. Lots of small back-and-forth is the signature of an
//!   indecisive cursor.
//! - **Click rate**: clicks per minute.
//! - **App-switch rate**: foreground-window changes per minute (fed by
//!   [`super::window_tracker::WindowTracker`]).
//! - **Idle**: seconds since the last input signal of any kind.

use crate::config::PerceptionConfig;
use crate::event_bus::EventBus;
use parking_lot::Mutex;
use std::collections::VecDeque;
use std::sync::Arc;
use std::time::Duration;
use tracing::{debug, warn};
use ultron_quantum_log::{EntryKind, NewEntry, QuantumLog};
use ultron_types::{InputMetrics, InputSignal, KeyCategory, UltronEvent};

#[derive(Debug, Clone, Copy)]
struct MouseSample {
    ts_ms: i64,
    dx: i32,
    dy: i32,
}

struct Inner {
    cfg: PerceptionConfig,
    /// Char-producing key-down timestamps (for WPM + rhythm).
    chars: VecDeque<i64>,
    /// Backspace key-down timestamps.
    backspaces: VecDeque<i64>,
    /// Mouse-button-down timestamps (any button).
    clicks: VecDeque<i64>,
    /// Mouse move samples (after the input_monitor's throttle).
    moves: VecDeque<MouseSample>,
    /// Foreground-window-change timestamps.
    window_switches: VecDeque<i64>,
    /// Most recent input signal of *any* kind (for idle).
    last_input_ms: i64,
}

#[derive(Clone)]
pub struct InputMetricsAggregator {
    inner: Arc<Mutex<Inner>>,
    bus: EventBus,
    qlog: QuantumLog,
}

impl InputMetricsAggregator {
    pub fn new(cfg: PerceptionConfig, bus: EventBus, qlog: QuantumLog) -> Self {
        Self {
            inner: Arc::new(Mutex::new(Inner {
                cfg,
                chars: VecDeque::new(),
                backspaces: VecDeque::new(),
                clicks: VecDeque::new(),
                moves: VecDeque::new(),
                window_switches: VecDeque::new(),
                last_input_ms: 0,
            })),
            bus,
            qlog,
        }
    }

    /// Feed a raw input signal in. Cheap: a few pushes onto deques.
    /// Called from the input_monitor forwarder, on the same path as
    /// the tension tracker.
    pub fn feed_input(&self, sig: &InputSignal) {
        let mut g = self.inner.lock();
        match sig {
            InputSignal::KeyEvent {
                ts_ms,
                category,
                is_down: true,
                ..
            } => {
                g.last_input_ms = *ts_ms;
                match category {
                    KeyCategory::Letter
                    | KeyCategory::Digit
                    | KeyCategory::Symbol
                    | KeyCategory::Whitespace => g.chars.push_back(*ts_ms),
                    KeyCategory::Backspace => {
                        g.chars.push_back(*ts_ms);
                        g.backspaces.push_back(*ts_ms);
                    }
                    _ => {}
                }
            }
            InputSignal::MouseButton {
                ts_ms,
                is_down: true,
                ..
            } => {
                g.last_input_ms = *ts_ms;
                g.clicks.push_back(*ts_ms);
            }
            InputSignal::MouseMove { ts_ms, dx, dy } => {
                g.last_input_ms = *ts_ms;
                g.moves.push_back(MouseSample {
                    ts_ms: *ts_ms,
                    dx: *dx,
                    dy: *dy,
                });
            }
            InputSignal::MouseScroll { ts_ms, .. } => {
                g.last_input_ms = *ts_ms;
            }
            InputSignal::Idle { .. } => {
                // Idle events are emitted by the monitor; we already
                // derive idleness from `last_input_ms`.
            }
            _ => {}
        }
    }

    /// Notify that the foreground window changed. Cheap.
    pub fn feed_window_change(&self, ts_ms: i64) {
        self.inner.lock().window_switches.push_back(ts_ms);
    }

    /// Compute and return a fresh [`InputMetrics`] snapshot. Also prunes
    /// stale entries out of the sliding deques. Cheap (linear in window).
    pub fn tick(&self) -> InputMetrics {
        let now_ms = chrono::Utc::now().timestamp_millis();
        let mut g = self.inner.lock();
        let win_ms = (g.cfg.metrics_window_secs as i64) * 1000;
        let cutoff = now_ms - win_ms;

        prune_ts(&mut g.chars, cutoff);
        prune_ts(&mut g.backspaces, cutoff);
        prune_ts(&mut g.clicks, cutoff);
        prune_ts(&mut g.window_switches, cutoff);
        prune_moves(&mut g.moves, cutoff);

        let win_secs = g.cfg.metrics_window_secs as f32;
        let scale_per_min = if win_secs > 0.0 { 60.0 / win_secs } else { 0.0 };

        // WPM: chars / 5 → words; words / window-mins = WPM.
        let wpm = (g.chars.len() as f32 / 5.0) * scale_per_min;
        let backspace_rate_per_min = (g.backspaces.len() as f32) * scale_per_min;
        let click_rate_per_min = (g.clicks.len() as f32) * scale_per_min;
        let app_switch_per_min = (g.window_switches.len() as f32) * scale_per_min;

        // Backspace storm: short-window check, independent of the metrics window.
        let storm_cutoff = now_ms - g.cfg.backspace_storm_window_ms as i64;
        let recent_bs = g
            .backspaces
            .iter()
            .rev()
            .take_while(|&&t| t >= storm_cutoff)
            .count();
        let backspace_storm = recent_bs >= g.cfg.backspace_storm_threshold;

        let typing_rhythm_variance = compute_rhythm_variance(&g.chars);
        let (mouse_velocity_px_per_sec, mouse_hesitation_score) =
            compute_mouse_metrics(&g.moves, win_secs);

        let idle_secs = if g.last_input_ms == 0 {
            // Never seen a signal yet — treat as long idle.
            win_secs
        } else {
            ((now_ms - g.last_input_ms) as f32 / 1000.0).max(0.0)
        };

        InputMetrics {
            wpm,
            backspace_rate_per_min,
            backspace_storm,
            typing_rhythm_variance,
            mouse_velocity_px_per_sec,
            mouse_hesitation_score,
            click_rate_per_min,
            app_switch_per_min,
            idle_secs,
            window_secs: win_secs,
            ts_unix_ms: now_ms,
        }
    }

    /// Spawn the periodic tick task. Returns the join handle so callers can
    /// abort on shutdown.
    pub fn spawn_ticker(self) -> tokio::task::JoinHandle<()> {
        let interval_ms = {
            let g = self.inner.lock();
            g.cfg.metrics_tick_ms.max(250)
        };
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_millis(interval_ms));
            interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            // Skip the immediate first tick — we want a moment to accumulate.
            interval.tick().await;
            let mut tick_count: u64 = 0;
            loop {
                interval.tick().await;
                let m = self.tick();
                self.bus.publish(UltronEvent::InputMetricsUpdated(m.clone()));
                tick_count = tick_count.wrapping_add(1);
                // Log every Nth tick to keep Quantum Log readable.
                let log_every = self.inner.lock().cfg.metrics_log_every_n_ticks.max(1);
                if tick_count % log_every == 0 {
                    let payload = serde_json::to_value(&m).unwrap_or(serde_json::json!({}));
                    if let Err(e) = self
                        .qlog
                        .append_async(NewEntry::new(
                            EntryKind::Event,
                            "perception/metrics",
                            payload,
                        ))
                        .await
                    {
                        warn!("qlog append failed in metrics aggregator: {e}");
                    }
                }
                debug!(
                    wpm = m.wpm,
                    bs_rate = m.backspace_rate_per_min,
                    storm = m.backspace_storm,
                    mouse_v = m.mouse_velocity_px_per_sec,
                    "metrics tick"
                );
            }
        })
    }
}

fn prune_ts(q: &mut VecDeque<i64>, cutoff_ms: i64) {
    while let Some(&front) = q.front() {
        if front < cutoff_ms {
            q.pop_front();
        } else {
            break;
        }
    }
}

fn prune_moves(q: &mut VecDeque<MouseSample>, cutoff_ms: i64) {
    while let Some(s) = q.front() {
        if s.ts_ms < cutoff_ms {
            q.pop_front();
        } else {
            break;
        }
    }
}

fn compute_rhythm_variance(keys: &VecDeque<i64>) -> f32 {
    if keys.len() < 4 {
        return 0.0;
    }
    let intervals: Vec<f32> = keys
        .iter()
        .zip(keys.iter().skip(1))
        .map(|(a, b)| (b - a) as f32)
        .filter(|x| *x >= 0.0)
        .collect();
    if intervals.is_empty() {
        return 0.0;
    }
    let mean = intervals.iter().sum::<f32>() / intervals.len() as f32;
    if mean <= 0.0 {
        return 0.0;
    }
    let var = intervals.iter().map(|x| (x - mean).powi(2)).sum::<f32>() / intervals.len() as f32;
    let std = var.sqrt();
    let cv = std / mean;
    clamp01(cv / 1.5)
}

/// Returns `(velocity_px_per_sec, hesitation_score_0_to_1)`.
fn compute_mouse_metrics(samples: &VecDeque<MouseSample>, win_secs: f32) -> (f32, f32) {
    if samples.is_empty() || win_secs <= 0.0 {
        return (0.0, 0.0);
    }
    // Total Euclidean distance.
    let mut total_dist = 0.0f32;
    // Direction reversal counter (per axis). A reversal = sign of dx or dy
    // flipped vs. the last *non-zero* sign.
    let mut reversals: u32 = 0;
    let mut prev_sx = 0i32;
    let mut prev_sy = 0i32;
    for s in samples {
        let dx = s.dx as f32;
        let dy = s.dy as f32;
        total_dist += (dx * dx + dy * dy).sqrt();

        let sx = s.dx.signum();
        let sy = s.dy.signum();
        if sx != 0 && prev_sx != 0 && sx != prev_sx {
            reversals += 1;
        }
        if sy != 0 && prev_sy != 0 && sy != prev_sy {
            reversals += 1;
        }
        if sx != 0 {
            prev_sx = sx;
        }
        if sy != 0 {
            prev_sy = sy;
        }
    }
    let velocity = total_dist / win_secs;
    let reversals_per_min = (reversals as f32) * (60.0 / win_secs);
    // 60 reversals/min ≈ saturated hesitation. Tunable.
    let hesitation = clamp01(reversals_per_min / 60.0);
    (velocity, hesitation)
}

fn clamp01(x: f32) -> f32 {
    if x.is_nan() {
        0.0
    } else {
        x.clamp(0.0, 1.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ultron_types::{KeyCategory, MouseButton};

    fn cfg() -> PerceptionConfig {
        PerceptionConfig::default()
    }

    fn temp_qlog() -> QuantumLog {
        let mut p = std::env::temp_dir();
        p.push(format!("ultron_metrics_test_{}.db", uuid::Uuid::new_v4()));
        QuantumLog::open(&p).unwrap()
    }

    fn key_down(ts: i64, cat: KeyCategory) -> InputSignal {
        InputSignal::KeyEvent {
            ts_ms: ts,
            category: cat,
            modifier_mask: 0,
            is_down: true,
        }
    }

    #[test]
    fn empty_metrics_are_zero() {
        let bus = EventBus::new(8);
        let agg = InputMetricsAggregator::new(cfg(), bus, temp_qlog());
        let m = agg.tick();
        assert_eq!(m.wpm, 0.0);
        assert_eq!(m.backspace_rate_per_min, 0.0);
        assert!(!m.backspace_storm);
        assert_eq!(m.click_rate_per_min, 0.0);
    }

    #[test]
    fn wpm_roughly_correct() {
        let bus = EventBus::new(8);
        let agg = InputMetricsAggregator::new(cfg(), bus, temp_qlog());
        let now = chrono::Utc::now().timestamp_millis();
        // 300 chars in the last 60s → 60 WPM (300 / 5 = 60).
        for i in 0..300 {
            agg.feed_input(&key_down(now - (i as i64) * 100, KeyCategory::Letter));
        }
        let m = agg.tick();
        assert!(m.wpm >= 55.0 && m.wpm <= 65.0, "wpm was {}", m.wpm);
    }

    #[test]
    fn backspace_storm_fires() {
        let bus = EventBus::new(8);
        let mut c = cfg();
        c.backspace_storm_threshold = 5;
        c.backspace_storm_window_ms = 3000;
        let agg = InputMetricsAggregator::new(c, bus, temp_qlog());
        let now = chrono::Utc::now().timestamp_millis();
        // 6 backspaces in the last 1.5s → storm.
        for i in 0..6 {
            agg.feed_input(&key_down(now - (i as i64) * 250, KeyCategory::Backspace));
        }
        let m = agg.tick();
        assert!(m.backspace_storm, "storm should be active");
        assert!(m.backspace_rate_per_min > 0.0);
    }

    #[test]
    fn no_storm_when_spread_out() {
        let bus = EventBus::new(8);
        let agg = InputMetricsAggregator::new(cfg(), bus, temp_qlog());
        let now = chrono::Utc::now().timestamp_millis();
        // 6 backspaces but spread over 30s — not a storm.
        for i in 0..6 {
            agg.feed_input(&key_down(now - (i as i64) * 5000, KeyCategory::Backspace));
        }
        let m = agg.tick();
        assert!(!m.backspace_storm);
    }

    #[test]
    fn click_rate_counted() {
        let bus = EventBus::new(8);
        let agg = InputMetricsAggregator::new(cfg(), bus, temp_qlog());
        let now = chrono::Utc::now().timestamp_millis();
        for i in 0..30 {
            agg.feed_input(&InputSignal::MouseButton {
                ts_ms: now - (i as i64) * 1000,
                button: MouseButton::Left,
                is_down: true,
            });
        }
        let m = agg.tick();
        assert!(m.click_rate_per_min >= 25.0, "rate was {}", m.click_rate_per_min);
    }

    #[test]
    fn idle_grows_with_no_input() {
        let bus = EventBus::new(8);
        let agg = InputMetricsAggregator::new(cfg(), bus, temp_qlog());
        let now = chrono::Utc::now().timestamp_millis();
        agg.feed_input(&key_down(now - 10_000, KeyCategory::Letter));
        let m = agg.tick();
        assert!(m.idle_secs >= 9.0 && m.idle_secs <= 11.5, "idle = {}", m.idle_secs);
    }

    #[test]
    fn mouse_reversals_drive_hesitation() {
        let bus = EventBus::new(8);
        let agg = InputMetricsAggregator::new(cfg(), bus, temp_qlog());
        let now = chrono::Utc::now().timestamp_millis();
        // Strong zig-zag: alternating dx sign every move.
        for i in 0..120 {
            let dx = if i % 2 == 0 { 5 } else { -5 };
            agg.feed_input(&InputSignal::MouseMove {
                ts_ms: now - (i as i64) * 250,
                dx,
                dy: 0,
            });
        }
        let m = agg.tick();
        assert!(m.mouse_hesitation_score > 0.5, "hesitation = {}", m.mouse_hesitation_score);
        assert!(m.mouse_velocity_px_per_sec > 0.0);
    }

    #[test]
    fn window_switches_counted() {
        let bus = EventBus::new(8);
        let agg = InputMetricsAggregator::new(cfg(), bus, temp_qlog());
        let now = chrono::Utc::now().timestamp_millis();
        // 12 switches inside the last 60s = 12 / min.
        for i in 0..12 {
            agg.feed_window_change(now - (i as i64) * 4000);
        }
        let m = agg.tick();
        assert!(m.app_switch_per_min >= 10.0 && m.app_switch_per_min <= 14.0);
    }
}
