//! # Tension Tracker
//!
//! Fuses input-derived signals into a single composite score in `[0.0, 1.0]`.
//! Drives behaviour gating across the system — when tension is high, modules
//! suppress non-critical interventions.
//!
//! ## Inputs
//! - **Typing volatility**: stddev of inter-keystroke intervals (recent window)
//! - **Click rate**: clicks/sec (recent window)
//! - **Error signal**: backspace burst rate (proxy for frustration)
//! - **Idle**: long idle time pulls the score down
//!
//! ## Update rule
//! Each tick (1 Hz), we compute an instantaneous target value `t`, then:
//!
//! ```text
//!     score' = (1 - α) * score + α * t
//! ```
//!
//! Then apply a small natural decay so that with no input, score → 0.
//!
//! Hysteresis on band edges prevents UI / voice flapping.

use crate::config::TensionConfig;
use crate::event_bus::EventBus;
use arc_swap::ArcSwap;
use parking_lot::Mutex;
use std::collections::VecDeque;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tracing::trace;
use ultron_types::{clamp01, EventEnvelope, InputSignal, KeyCategory, TensionBand, TensionSnapshot, UltronEvent};

const TYPING_WINDOW_MS: u64 = 5_000;
const CLICK_WINDOW_MS: u64 = 5_000;
const ERROR_WINDOW_MS: u64 = 8_000;

struct Inner {
    cfg: TensionConfig,
    score: f32,
    last_band: TensionBand,
    last_input: Instant,
    keys: VecDeque<i64>,           // ts_ms of keypresses (any letter/digit/symbol)
    clicks: VecDeque<i64>,         // ts_ms of mouse button presses
    backspaces: VecDeque<i64>,     // ts_ms of backspace presses
}

#[derive(Clone)]
pub struct TensionTracker {
    inner: Arc<Mutex<Inner>>,
    snapshot: Arc<ArcSwap<TensionSnapshot>>,
    bus: EventBus,
}

impl TensionTracker {
    pub fn new(cfg: TensionConfig, bus: EventBus) -> Self {
        let snap = TensionSnapshot::default();
        Self {
            inner: Arc::new(Mutex::new(Inner {
                cfg,
                score: 0.0,
                last_band: TensionBand::Calm,
                last_input: Instant::now(),
                keys: VecDeque::new(),
                clicks: VecDeque::new(),
                backspaces: VecDeque::new(),
            })),
            snapshot: Arc::new(ArcSwap::from_pointee(snap)),
            bus,
        }
    }

    /// Lock-free read of the latest snapshot.
    pub fn snapshot(&self) -> TensionSnapshot {
        **self.snapshot.load()
    }

    pub fn current(&self) -> f32 {
        self.snapshot().value
    }

    /// Feed a single input signal in.
    pub fn feed(&self, sig: &InputSignal) {
        let mut g = self.inner.lock();
        g.last_input = Instant::now();
        match sig {
            InputSignal::KeyEvent {
                ts_ms,
                category,
                is_down: true,
                ..
            } => {
                match category {
                    KeyCategory::Letter
                    | KeyCategory::Digit
                    | KeyCategory::Symbol
                    | KeyCategory::Whitespace => g.keys.push_back(*ts_ms),
                    KeyCategory::Backspace => {
                        g.keys.push_back(*ts_ms);
                        g.backspaces.push_back(*ts_ms);
                    }
                    _ => {}
                }
            }
            InputSignal::MouseButton {
                ts_ms,
                is_down: true,
                ..
            } => g.clicks.push_back(*ts_ms),
            _ => {}
        }
    }

    /// Run a tick (recompute score). Should be called ~1 Hz from the runtime.
    /// Returns the new snapshot.
    pub fn tick(&self) -> TensionSnapshot {
        let now_ms = chrono::Utc::now().timestamp_millis();
        let mut g = self.inner.lock();

        prune_older_than(&mut g.keys, now_ms - TYPING_WINDOW_MS as i64);
        prune_older_than(&mut g.clicks, now_ms - CLICK_WINDOW_MS as i64);
        prune_older_than(&mut g.backspaces, now_ms - ERROR_WINDOW_MS as i64);

        let typing_volatility = compute_typing_volatility(&g.keys);
        let click_rate = clamp01((g.clicks.len() as f32) / 12.0);
        let error_signal = clamp01((g.backspaces.len() as f32) / 8.0);

        let idle_secs = g.last_input.elapsed().as_secs_f32();
        let idle_pull = clamp01(idle_secs / 60.0); // pulls tension DOWN

        let cfg = g.cfg;
        let target = clamp01(
            cfg.w_typing_volatility * typing_volatility
                + cfg.w_click_rate * click_rate
                + cfg.w_error_signal * error_signal
                - cfg.w_idle * idle_pull,
        );

        // EWMA update.
        g.score = (1.0 - cfg.ewma_alpha) * g.score + cfg.ewma_alpha * target;
        // Natural decay each tick.
        g.score = (g.score - cfg.decay_per_sec).max(0.0);
        let v = clamp01(g.score);

        let new_band = TensionBand::from_value(v);
        let prev_band = g.last_band;
        g.last_band = new_band;
        let prev_value = self.snapshot().value;

        let snap = TensionSnapshot {
            value: v,
            typing_volatility,
            click_rate,
            idle_secs,
            error_signal,
            ts_unix_ms: now_ms,
        };
        self.snapshot.store(Arc::new(snap));
        drop(g);

        if new_band != prev_band {
            trace!(?prev_band, ?new_band, value = v, "tension band changed");
            self.bus.publish(UltronEvent::TensionChanged {
                previous: prev_value,
                current: v,
            });
            // Fix 3 — on a rising transition into Spiked, request a screenshot
            // so Module O has visual context for the high-tension moment.
            // The Screenshotter's listener handles the actual capture; we
            // only publish the intent. This is intentionally an
            // `UltronEvent::Custom` rather than a first-class variant so
            // other listeners (HUD, agent router) can subscribe to the same
            // signal without us inventing a `RequestScreenshot` event type.
            if new_band == TensionBand::Spiked && prev_band != TensionBand::Spiked {
                self.bus.publish(UltronEvent::Custom(EventEnvelope::new(
                    "tension",
                    "request_screenshot",
                    serde_json::json!({ "reason": "high_tension", "value": v }),
                )));
            }
        }
        snap
    }

    /// Spawn a background task that calls `tick()` once per second.
    pub fn spawn_ticker(self) -> tokio::task::JoinHandle<()> {
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(1));
            interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            loop {
                interval.tick().await;
                self.tick();
            }
        })
    }
}

fn prune_older_than(q: &mut VecDeque<i64>, cutoff_ms: i64) {
    while let Some(&front) = q.front() {
        if front < cutoff_ms {
            q.pop_front();
        } else {
            break;
        }
    }
}

fn compute_typing_volatility(keys: &VecDeque<i64>) -> f32 {
    if keys.len() < 4 {
        return 0.0;
    }
    let intervals: Vec<f32> = keys
        .iter()
        .zip(keys.iter().skip(1))
        .map(|(a, b)| (b - a) as f32)
        .collect();
    let mean = intervals.iter().sum::<f32>() / intervals.len() as f32;
    if mean <= 0.0 {
        return 0.0;
    }
    let var = intervals
        .iter()
        .map(|x| (x - mean).powi(2))
        .sum::<f32>()
        / intervals.len() as f32;
    let std = var.sqrt();
    let coef_of_var = std / mean;
    // Compress to [0, 1] — roughly: cv > 1.5 saturates.
    clamp01(coef_of_var / 1.5)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> TensionConfig {
        TensionConfig {
            ewma_alpha: 0.5,
            decay_per_sec: 0.0,
            w_typing_volatility: 0.4,
            w_click_rate: 0.2,
            w_error_signal: 0.4,
            w_idle: 0.1,
        }
    }

    #[test]
    fn idle_drives_to_zero() {
        let bus = EventBus::new(8);
        let t = TensionTracker::new(cfg(), bus);
        for _ in 0..30 {
            t.tick();
        }
        assert!(t.current() < 0.05);
    }

    #[test]
    fn backspace_storm_raises_score() {
        let bus = EventBus::new(8);
        let t = TensionTracker::new(cfg(), bus);
        let now = chrono::Utc::now().timestamp_millis();
        for i in 0..16 {
            t.feed(&InputSignal::KeyEvent {
                ts_ms: now - (i as i64) * 50,
                category: KeyCategory::Backspace,
                modifier_mask: 0,
                is_down: true,
            });
        }
        let s = t.tick();
        assert!(s.error_signal > 0.5, "error_signal was {}", s.error_signal);
        assert!(s.value > 0.15, "score was {}", s.value);
    }
}
