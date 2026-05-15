//! Mutable state held by the Module-O sidecar between ticks.
//!
//! Every event we subscribe to lands here via a typed handler method; the
//! fusion loop reads a *snapshot* of this state every 5 s and produces an
//! [`InsightSnapshot`].
//!
//! ## Concurrency model
//!
//! The whole struct is wrapped in `Arc<Mutex<SignalState>>` at the caller
//! (`main.rs`). Lock-hold times are intentionally short — handler methods
//! mutate a single field, fusion clones small pieces out. No async work
//! ever happens with the lock held.
//!
//! ## What this struct does **not** do
//!
//! - It does **not** compute `cognitive_load` or any other derived value.
//!   That's `fusion::assemble`'s job.
//! - It does **not** publish events or touch the Quantum Log. Side effects
//!   live in `main.rs` / `ws_client.rs`.

use std::collections::VecDeque;
use ultron_types::{AppCategory, InputMetrics, ProductivityPriorUpdate, TensionBand};

/// 5-minute rolling history horizon for tension samples and focus
/// switches. Long enough to compute `focus_switch_rate` over the
/// "recent past" without dragging in stale data.
const FIVE_MIN_MS: i64 = 5 * 60 * 1_000;

/// 60-second horizon for the `tension_trend` lookback used by Signal 1.
pub const TENSION_TREND_LOOKBACK_MS: i64 = 60 * 1_000;

#[derive(Debug, Clone)]
pub struct SignalState {
    // ── Signal 1: Tension ──────────────────────────────────────────────
    pub tension: f32,
    pub tension_band: TensionBand,
    /// `(ts_unix_ms, tension)` ring — 5 minutes deep.
    pub tension_history: VecDeque<(i64, f32)>,

    // ── Signal 2: Focus context ────────────────────────────────────────
    pub focus_app: String,
    pub focus_category: AppCategory,
    /// When the current foreground window first became foreground, in
    /// Unix-ms. `0` means "no window seen yet".
    pub focus_since_ms: i64,
    /// `ts_unix_ms` of each foreground-window change in the last 5 min.
    pub focus_switches: VecDeque<i64>,

    // ── Signal 3: Input cadence ────────────────────────────────────────
    /// Most recent `InputMetrics` snapshot we've received from H. `None`
    /// until the first `InputMetricsUpdated` arrives.
    pub last_metrics: Option<InputMetrics>,

    // ── Signal 4: Visual context ───────────────────────────────────────
    pub visual_label: Option<String>,
    pub visual_label_ts: Option<i64>,

    // ── Signal 5 / 7 — learned productivity prior (Module D) ──────────
    /// Latest `ProductivityPriorUpdate` from Module D, if any. `None`
    /// means D isn't running or hasn't published anything yet — fusion
    /// falls back to the circadian default in that case.
    pub learned_priors: Option<ProductivityPriorUpdate>,

    // ── Bookkeeping ────────────────────────────────────────────────────
    pub tick: u64,
}

impl Default for SignalState {
    fn default() -> Self {
        Self {
            tension: 0.0,
            tension_band: TensionBand::Calm,
            tension_history: VecDeque::new(),
            focus_app: String::new(),
            focus_category: AppCategory::Unknown,
            focus_since_ms: 0,
            focus_switches: VecDeque::new(),
            last_metrics: None,
            visual_label: None,
            visual_label_ts: None,
            learned_priors: None,
            tick: 0,
        }
    }
}

impl SignalState {
    pub fn new() -> Self {
        Self::default()
    }

    // ---- event handlers --------------------------------------------------

    /// `tension_changed`. Updates the snapshot value, pushes history,
    /// recomputes the band. `now_ms` is injectable for tests.
    pub fn on_tension_changed(&mut self, current: f32, now_ms: i64) {
        self.tension = current;
        self.tension_band = TensionBand::from_value(current);
        self.push_tension_history(now_ms, current);
    }

    /// `heartbeat`. The daemon emits one every few seconds with the
    /// current tension; we use this as a fallback in case the tracker
    /// goes a long time without a band transition (which silences
    /// `TensionChanged`). Idempotent — only updates if the value moved.
    pub fn on_heartbeat(&mut self, tension: f32, now_ms: i64) {
        // Only push if we're seeing a fresh-enough value, otherwise we'd
        // pad the history with duplicates.
        let stale = self
            .tension_history
            .back()
            .map(|(t, _)| (now_ms - t) > 4_000)
            .unwrap_or(true);
        self.tension = tension;
        self.tension_band = TensionBand::from_value(tension);
        if stale {
            self.push_tension_history(now_ms, tension);
        }
    }

    /// `input_metrics_updated`.
    pub fn on_input_metrics(&mut self, m: InputMetrics) {
        self.last_metrics = Some(m);
    }

    /// `window_changed`.
    pub fn on_window_changed(
        &mut self,
        process_name: String,
        app_category: Option<AppCategory>,
        ts_unix_ms: i64,
    ) {
        // Same-process titles (e.g. browser tab switch on same HWND) still
        // raise this event because the daemon emits on title-only changes.
        // We treat them as a switch only when the *process* changes; that
        // matches the build prompt's definition of `focus_duration_secs`.
        let process_changed = process_name != self.focus_app;
        self.focus_app = process_name;
        self.focus_category = app_category.unwrap_or(AppCategory::Unknown);
        if process_changed || self.focus_since_ms == 0 {
            self.focus_since_ms = ts_unix_ms;
        }
        self.focus_switches.push_back(ts_unix_ms);
        self.prune_focus_switches(ts_unix_ms);
    }

    /// `custom` event with `kind == "visual_label"`. Expected payload:
    /// `{"label": "...", "screenshot_ts": <ms>}`. `screenshot_ts` is
    /// preferred over our local clock so consumers can correctly age the
    /// label against when the *frame* was captured.
    pub fn on_visual_label(&mut self, label: String, screenshot_ts_ms: Option<i64>, now_ms: i64) {
        let label = label.trim();
        if label.is_empty() {
            return;
        }
        self.visual_label = Some(label.to_string());
        self.visual_label_ts = Some(screenshot_ts_ms.unwrap_or(now_ms));
    }

    /// `custom` event with `kind == "productivity_prior_update"`.
    /// Stores the latest learned curve from Module D. Fusion reads
    /// `learned_priors` on each tick and prefers the learned value
    /// over the circadian default when the current hour has data.
    ///
    /// We blindly accept whatever D sends — D itself does the
    /// min-samples gating before publishing. Schema-version mismatches
    /// are silently dropped so a future schema rev can't crash O.
    pub fn on_productivity_prior_update(&mut self, update: ProductivityPriorUpdate) {
        if update.schema_version != ultron_types::PRIOR_SCHEMA_VERSION {
            // Wrong version — refuse rather than guess.
            return;
        }
        self.learned_priors = Some(update);
    }

    /// Read the learned prior for `hour`, if any. Returns `None` when
    /// D hasn't published, or for an hour where D hasn't accumulated
    /// enough samples yet.
    pub fn learned_prior_for_hour(&self, hour: u32) -> Option<f32> {
        if hour > 23 {
            return None;
        }
        self.learned_priors
            .as_ref()
            .and_then(|u| u.priors[hour as usize])
    }

    // ---- accessors used by fusion ---------------------------------------

    /// Number of seconds since the current foreground window first
    /// gained focus. `0` if we haven't seen a window yet.
    pub fn focus_duration_secs(&self, now_ms: i64) -> u64 {
        if self.focus_since_ms == 0 {
            return 0;
        }
        ((now_ms - self.focus_since_ms).max(0) / 1_000) as u64
    }

    /// Foreground-window switches per minute, averaged over the last
    /// 5 minutes.
    pub fn focus_switch_rate(&self, now_ms: i64) -> f32 {
        // Use the rolling 5-min window; cheap because we prune on insert.
        let cutoff = now_ms - FIVE_MIN_MS;
        let n = self.focus_switches.iter().filter(|t| **t >= cutoff).count();
        // Per-minute = count / 5 (because window is 5 min).
        (n as f32) / 5.0
    }

    /// Tension delta vs. ~60 s ago. `0.0` when we don't have enough
    /// history. We search for the *closest* sample to (now - 60s) rather
    /// than requiring an exact hit — heartbeats arrive every 5 s so there
    /// will always be one within a few seconds of the target.
    pub fn tension_trend(&self, now_ms: i64) -> f32 {
        let target = now_ms - TENSION_TREND_LOOKBACK_MS;
        if self.tension_history.is_empty() {
            return 0.0;
        }
        // Refuse to trend when our oldest sample is newer than the lookback;
        // any "trend" we'd report would just be reflecting our own bootstrap.
        let oldest = self.tension_history.front().map(|(t, _)| *t).unwrap_or(now_ms);
        if oldest > target {
            return 0.0;
        }
        // Pick the sample with the smallest |ts - target|.
        let best = self
            .tension_history
            .iter()
            .min_by_key(|(t, _)| (*t - target).abs())
            .map(|(_, v)| *v);
        match best {
            Some(v) => self.tension - v,
            None => 0.0,
        }
    }

    // ---- internal pruning -----------------------------------------------

    fn push_tension_history(&mut self, ts_ms: i64, value: f32) {
        self.tension_history.push_back((ts_ms, value));
        let cutoff = ts_ms - FIVE_MIN_MS;
        while let Some(&(t, _)) = self.tension_history.front() {
            if t < cutoff {
                self.tension_history.pop_front();
            } else {
                break;
            }
        }
    }

    fn prune_focus_switches(&mut self, now_ms: i64) {
        let cutoff = now_ms - FIVE_MIN_MS;
        while let Some(&front) = self.focus_switches.front() {
            if front < cutoff {
                self.focus_switches.pop_front();
            } else {
                break;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s() -> SignalState {
        SignalState::new()
    }

    #[test]
    fn fresh_state_is_calm() {
        let s = s();
        assert_eq!(s.tension, 0.0);
        assert_eq!(s.tension_band, TensionBand::Calm);
        assert_eq!(s.focus_duration_secs(1_000), 0);
        assert_eq!(s.focus_switch_rate(1_000), 0.0);
    }

    #[test]
    fn window_change_starts_focus_clock() {
        let mut s = s();
        s.on_window_changed("Code.exe".into(), Some(AppCategory::Coding), 1_000);
        assert_eq!(s.focus_app, "Code.exe");
        assert_eq!(s.focus_category, AppCategory::Coding);
        assert_eq!(s.focus_since_ms, 1_000);
        assert_eq!(s.focus_duration_secs(61_000), 60);
    }

    #[test]
    fn same_process_does_not_reset_focus_clock() {
        // A title change inside the same process (e.g. browser tab
        // navigation) must NOT reset focus_since_ms.
        let mut s = s();
        s.on_window_changed("chrome.exe".into(), Some(AppCategory::Browser), 1_000);
        s.on_window_changed("chrome.exe".into(), Some(AppCategory::Browser), 5_000);
        assert_eq!(s.focus_since_ms, 1_000, "title change must not reset");
    }

    #[test]
    fn switch_rate_over_5min_window() {
        let mut s = s();
        // 10 distinct apps in the last 5 min = 2/min.
        for i in 0..10 {
            let name = format!("app{i}.exe");
            s.on_window_changed(name, Some(AppCategory::Unknown), i * 25_000);
        }
        let rate = s.focus_switch_rate(10 * 25_000);
        assert!((rate - 2.0).abs() < 0.01, "rate = {rate}");
    }

    #[test]
    fn tension_trend_zero_with_no_history() {
        let s = s();
        assert_eq!(s.tension_trend(0), 0.0);
    }

    #[test]
    fn tension_trend_positive_when_rising() {
        let mut s = s();
        // 60s ago: 0.2; now: 0.7. Trend should be ~+0.5.
        s.on_tension_changed(0.2, 0);
        s.on_tension_changed(0.7, 60_000);
        let trend = s.tension_trend(60_000);
        assert!((trend - 0.5).abs() < 0.01, "trend = {trend}");
    }

    #[test]
    fn visual_label_empty_ignored() {
        let mut s = s();
        s.on_visual_label("   ".into(), Some(10), 100);
        assert_eq!(s.visual_label, None);
    }

    #[test]
    fn learned_prior_stored_and_read() {
        let mut s = s();
        let mut u = ProductivityPriorUpdate::empty(1_000);
        u.priors[9] = Some(0.82);
        u.sample_counts[9] = 100;
        s.on_productivity_prior_update(u);
        assert_eq!(s.learned_prior_for_hour(9), Some(0.82));
        assert_eq!(s.learned_prior_for_hour(10), None);
        assert_eq!(s.learned_prior_for_hour(24), None);
    }

    #[test]
    fn learned_prior_rejects_wrong_schema_version() {
        let mut s = s();
        let mut u = ProductivityPriorUpdate::empty(1_000);
        u.priors[9] = Some(0.82);
        u.schema_version = 99; // future / unknown
        s.on_productivity_prior_update(u);
        assert!(s.learned_priors.is_none(), "wrong schema must be dropped");
        assert_eq!(s.learned_prior_for_hour(9), None);
    }
}
