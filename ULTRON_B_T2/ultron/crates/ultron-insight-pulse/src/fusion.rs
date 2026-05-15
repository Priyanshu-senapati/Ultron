//! Pure fusion: [`SignalState`] + clock + config → [`InsightSnapshot`].
//!
//! Every formula lives here. Side effects (publishing the snapshot, logging
//! to the Quantum Log, etc.) live in `main.rs` — this module deliberately
//! has no I/O at all so the unit tests are deterministic.
//!
//! ## Why the formulas are duplicated between `ultron-types` and here
//!
//! Two of them aren't: [`compute_cognitive_load`] lives in `ultron-types`
//! so every consumer agrees on the value. The thresholds inside this file
//! (cadence bands, focus-score linearisation, fatigue trigger) are also
//! re-exported through `ultron-types::insight` (e.g. `CadenceBand::classify`)
//! and we delegate to those. Anything that *looks* duplicated is actually a
//! call into the shared crate.

use crate::circadian;
use crate::signal_state::SignalState;
use ultron_types::{
    clamp01, compute_cognitive_load, AppCategory, CadenceBand, InsightSnapshot, TensionBand,
};

/// Maximum age (in seconds) at which a visual label is still considered
/// fresh. Anything older is dropped from the snapshot. Mirrors
/// `InsightConfig.visual_label_max_age_secs`; passed in rather than read
/// here to keep this module pure.
pub const DEFAULT_VISUAL_LABEL_MAX_AGE_SECS: u32 = 120;

/// Fatigue trigger: focus on `Coding` for at least this many seconds.
pub const FATIGUE_CODING_THRESHOLD_SECS: u64 = 3 * 60 * 60; // 3 hours

/// `focus_score` saturates at this many switches per minute (i.e. 6+
/// switches/min = score 0.0).
pub const FOCUS_SCORE_MAX_SWITCH_RATE: f32 = 6.0;

/// Inputs to fusion that aren't part of the persistent `SignalState`. Wrapped
/// in a struct so the caller can pass everything in one go.
#[derive(Debug, Clone, Copy)]
pub struct FusionInputs {
    pub now_ms: i64,
    pub current_hour_local: Option<u32>,
    pub visual_label_max_age_secs: u32,
}

impl FusionInputs {
    /// Default-construct from the system clock. Tests bypass this and
    /// build the struct directly with frozen values.
    pub fn from_now() -> Self {
        let now_local = chrono::Local::now();
        Self {
            now_ms: chrono::Utc::now().timestamp_millis(),
            current_hour_local: Some(chrono::Timelike::hour(&now_local)),
            visual_label_max_age_secs: DEFAULT_VISUAL_LABEL_MAX_AGE_SECS,
        }
    }
}

/// Build an [`InsightSnapshot`] from the current signal state.
///
/// Pure: no clock, no config read, no logging. Caller passes a
/// [`FusionInputs`] capturing every external observation.
pub fn assemble(state: &SignalState, inputs: FusionInputs) -> InsightSnapshot {
    // ── Signal 1: Tension ──────────────────────────────────────────────
    let tension = clamp01(state.tension);
    let tension_band = state.tension_band;
    let tension_trend = state.tension_trend(inputs.now_ms);

    // ── Signal 2: Focus context ────────────────────────────────────────
    let focus_app = state.focus_app.clone();
    let focus_category = state.focus_category;
    let focus_duration_secs = state.focus_duration_secs(inputs.now_ms);
    let focus_switch_rate = state.focus_switch_rate(inputs.now_ms);
    let focus_score = compute_focus_score(focus_switch_rate);
    let fatigue_flag = focus_category == AppCategory::Coding
        && focus_duration_secs >= FATIGUE_CODING_THRESHOLD_SECS;

    // ── Signal 3: Input cadence ────────────────────────────────────────
    let metrics = state.last_metrics.as_ref();
    let wpm = metrics.map(|m| m.wpm).unwrap_or(0.0);
    let wpm_slope_per_hour = metrics.map(|m| m.wpm_slope_per_hour).unwrap_or(0.0);
    let backspace_storm = metrics.map(|m| m.backspace_storm).unwrap_or(false);
    let typing_rhythm_variance = metrics.map(|m| m.typing_rhythm_variance).unwrap_or(0.0);
    let mouse_hesitation_score = metrics.map(|m| m.mouse_hesitation_score).unwrap_or(0.0);
    let cadence_band = CadenceBand::classify(wpm, backspace_storm);

    // ── Signal 4: Visual context (with staleness drop) ─────────────────
    let (visual_label, visual_label_age_secs) =
        resolve_visual_label(state, inputs.now_ms, inputs.visual_label_max_age_secs);

    // ── Signal 5: Circadian ────────────────────────────────────────────
    let phase = inputs
        .current_hour_local
        .map(circadian::phase_at)
        .unwrap_or_else(circadian::current_phase);
    // Prefer Module-D's learned per-hour prior when available. Fall
    // back to the static circadian default when D hasn't learned this
    // hour yet (or isn't running at all). If we don't know the current
    // hour, we can't index the curve, so the default also wins.
    let productivity_prior = inputs
        .current_hour_local
        .and_then(|h| state.learned_prior_for_hour(h))
        .unwrap_or_else(|| phase.default_productivity_prior());

    // ── Derived composite ──────────────────────────────────────────────
    let cognitive_load = compute_cognitive_load(
        tension,
        focus_score,
        typing_rhythm_variance,
        mouse_hesitation_score,
        backspace_storm,
    );

    InsightSnapshot {
        tick: state.tick,
        ts_unix_ms: inputs.now_ms,
        tension,
        tension_band,
        tension_trend,
        focus_app,
        focus_category,
        focus_duration_secs,
        focus_switch_rate,
        focus_score,
        fatigue_flag,
        wpm,
        wpm_slope_per_hour,
        backspace_storm,
        typing_rhythm_variance,
        mouse_hesitation_score,
        cadence_band,
        visual_label,
        visual_label_age_secs,
        circadian_phase: phase,
        productivity_prior,
        cognitive_load,
    }
}

/// `clamp01(1.0 - switch_rate / 6.0)`. 6+ switches/min ⇒ 0.0.
#[inline]
fn compute_focus_score(switch_rate: f32) -> f32 {
    clamp01(1.0 - switch_rate / FOCUS_SCORE_MAX_SWITCH_RATE)
}

/// Returns `(label_for_snapshot, age_in_seconds)`. Drops the label (sets
/// it to `None`) once it crosses `max_age_secs` — stale visual context is
/// worse than no context. The age is *always* reported truthfully so the
/// Quantum Log can record "we suppressed a stale label of age N".
fn resolve_visual_label(
    state: &SignalState,
    now_ms: i64,
    max_age_secs: u32,
) -> (Option<String>, u32) {
    let Some(label) = state.visual_label.as_ref() else {
        return (None, 0);
    };
    let Some(ts) = state.visual_label_ts else {
        return (Some(label.clone()), 0);
    };
    let age_ms = (now_ms - ts).max(0);
    let age_secs = (age_ms / 1_000).min(u32::MAX as i64) as u32;
    if age_secs > max_age_secs {
        (None, age_secs)
    } else {
        (Some(label.clone()), age_secs)
    }
}

// =====================================================================
// Tests — the 8 cases specified in the Module-O build prompt.
// =====================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use ultron_types::{CircadianPhase, InputMetrics};

    fn base_inputs() -> FusionInputs {
        FusionInputs {
            now_ms: 1_000_000,
            current_hour_local: Some(10), // morning, deterministic
            visual_label_max_age_secs: 120,
        }
    }

    /// Test 1 — empty SignalState → calm snapshot, cognitive_load < 0.1.
    #[test]
    fn assemble_with_no_data_returns_calm_snapshot() {
        let s = SignalState::new();
        let snap = assemble(&s, base_inputs());
        assert_eq!(snap.tension, 0.0);
        assert_eq!(snap.tension_band, TensionBand::Calm);
        assert!(
            snap.cognitive_load < 0.1,
            "cognitive_load = {}",
            snap.cognitive_load
        );
        // With empty state, focus_score should be 1.0 (no switches), so
        // `(1 - focus_score) * 0.25 = 0` — confirms our zero baseline.
        assert!((snap.focus_score - 1.0).abs() < 1e-6);
    }

    /// Test 2 — known inputs produce the expected cognitive_load.
    #[test]
    fn cognitive_load_formula_is_correct() {
        // tension=0.5, focus_score=0.4, rhythm=0.2, hes=0.5, storm=true
        // = 0.5*0.40 + 0.6*0.25 + 0.2*0.15 + 0.5*0.10 + 0.10 = 0.53
        //
        // We can't set focus_score directly — drive it via switch rate.
        // 3.6 switches/min → focus_score = clamp01(1 - 3.6/6) = 0.4.
        let mut s = SignalState::new();
        s.tension = 0.5;
        s.tension_band = TensionBand::Loaded;
        // 18 switches over 5 minutes = 3.6/min.
        for i in 0..18 {
            s.on_window_changed("x.exe".into(), Some(AppCategory::Unknown), (i as i64) * 16_000);
            // Force distinct process names so each one counts as a switch.
            s.focus_app = format!("p{i}.exe");
            s.focus_switches.push_back((i as i64) * 16_000);
        }
        s.last_metrics = Some(InputMetrics {
            typing_rhythm_variance: 0.2,
            mouse_hesitation_score: 0.5,
            backspace_storm: true,
            ..InputMetrics::default()
        });
        let inputs = FusionInputs {
            now_ms: 17 * 16_000,
            ..base_inputs()
        };
        let snap = assemble(&s, inputs);
        assert!(
            (snap.focus_score - 0.4).abs() < 0.05,
            "focus_score = {}",
            snap.focus_score
        );
        assert!(
            (snap.cognitive_load - 0.53).abs() < 0.05,
            "cognitive_load = {}",
            snap.cognitive_load
        );
    }

    /// Test 3 — 12 switches/min flattens focus_score to 0.0.
    #[test]
    fn focus_score_clamps_at_zero_with_high_switch_rate() {
        let mut s = SignalState::new();
        // 60 switches in 5 minutes = 12/min.
        for i in 0..60 {
            s.focus_switches.push_back((i as i64) * 5_000);
        }
        let inputs = FusionInputs {
            now_ms: 60 * 5_000,
            ..base_inputs()
        };
        let snap = assemble(&s, inputs);
        assert_eq!(snap.focus_score, 0.0, "focus_score should clamp to 0");
    }

    /// Test 4 — label age 150s > threshold 120s → label dropped.
    #[test]
    fn visual_label_cleared_when_stale() {
        let mut s = SignalState::new();
        s.visual_label = Some("editing code".into());
        // Captured 150s before now_ms.
        s.visual_label_ts = Some(base_inputs().now_ms - 150_000);
        let snap = assemble(&s, base_inputs());
        assert_eq!(snap.visual_label, None);
        // Age is still reported truthfully (~150).
        assert!(snap.visual_label_age_secs >= 149 && snap.visual_label_age_secs <= 151);
    }

    /// Test 5 — tension 0.2 (60s ago) → 0.7 (now) → trend ≈ 0.5.
    #[test]
    fn tension_trend_positive_when_rising() {
        let mut s = SignalState::new();
        let now_ms = 60_000;
        s.on_tension_changed(0.2, 0);
        s.on_tension_changed(0.7, now_ms);
        let inputs = FusionInputs {
            now_ms,
            ..base_inputs()
        };
        let snap = assemble(&s, inputs);
        assert!(
            (snap.tension_trend - 0.5).abs() < 0.01,
            "trend = {}",
            snap.tension_trend
        );
    }

    /// Test 6 — circadian phases at the boundaries.
    #[test]
    fn circadian_phase_correct_at_boundaries() {
        // 08:00 → Morning; 12:00 → Afternoon; 21:00 → Night; 00:00 → LateNight.
        let s = SignalState::new();
        for (hour, want) in &[
            (8u32, CircadianPhase::Morning),
            (12, CircadianPhase::Afternoon),
            (21, CircadianPhase::Night),
            (0, CircadianPhase::LateNight),
        ] {
            let inputs = FusionInputs {
                current_hour_local: Some(*hour),
                ..base_inputs()
            };
            let snap = assemble(&s, inputs);
            assert_eq!(snap.circadian_phase, *want, "hour {hour}");
        }
    }

    /// Test 7 — Coding + ≥3h duration → fatigue_flag set.
    #[test]
    fn fatigue_flag_set_after_3h_coding() {
        let mut s = SignalState::new();
        // Foreground became Code.exe at t=0; now_ms = 10801s = 3h0m1s later.
        s.focus_app = "Code.exe".into();
        s.focus_category = AppCategory::Coding;
        s.focus_since_ms = 0;
        let inputs = FusionInputs {
            now_ms: 10_801 * 1_000,
            ..base_inputs()
        };
        let snap = assemble(&s, inputs);
        assert!(snap.fatigue_flag, "fatigue flag should be set");
        assert_eq!(snap.focus_duration_secs, 10_801);
    }

    /// Test 8 — backspace_storm forces Frenetic even at low WPM.
    #[test]
    fn cadence_band_frenetic_on_storm() {
        let mut s = SignalState::new();
        s.last_metrics = Some(InputMetrics {
            wpm: 20.0,
            backspace_storm: true,
            ..InputMetrics::default()
        });
        let snap = assemble(&s, base_inputs());
        assert_eq!(snap.cadence_band, CadenceBand::Frenetic);
    }

    // ── Bonus regression tests beyond the spec ──────────────────────────

    #[test]
    fn cadence_band_idle_with_no_metrics() {
        let s = SignalState::new();
        let snap = assemble(&s, base_inputs());
        assert_eq!(snap.cadence_band, CadenceBand::Idle);
    }

    #[test]
    fn focus_duration_zero_with_no_window_seen() {
        let s = SignalState::new();
        let snap = assemble(&s, base_inputs());
        assert_eq!(snap.focus_duration_secs, 0);
        assert!(!snap.fatigue_flag);
    }

    // ── Module D integration: learned productivity prior ─────────────────

    #[test]
    fn productivity_prior_uses_circadian_default_when_no_learning() {
        // Hour 10 → Morning → default is 0.85.
        let s = SignalState::new();
        let snap = assemble(&s, base_inputs());
        assert!(
            (snap.productivity_prior - 0.85).abs() < 1e-4,
            "expected Morning default 0.85, got {}",
            snap.productivity_prior
        );
    }

    #[test]
    fn productivity_prior_uses_learned_value_when_available() {
        let mut s = SignalState::new();
        let mut u = ultron_types::ProductivityPriorUpdate::empty(1_000);
        u.priors[10] = Some(0.42); // overrides the Morning default
        s.on_productivity_prior_update(u);
        let snap = assemble(&s, base_inputs());
        assert!(
            (snap.productivity_prior - 0.42).abs() < 1e-4,
            "expected learned 0.42, got {}",
            snap.productivity_prior
        );
    }

    #[test]
    fn productivity_prior_falls_back_for_unlearned_hour() {
        // We learn hour 10 only. Build inputs for hour 14 → Afternoon
        // (default 0.65) — learning should NOT spill across hours.
        let mut s = SignalState::new();
        let mut u = ultron_types::ProductivityPriorUpdate::empty(1_000);
        u.priors[10] = Some(0.42);
        s.on_productivity_prior_update(u);
        let inputs = FusionInputs {
            current_hour_local: Some(14),
            ..base_inputs()
        };
        let snap = assemble(&s, inputs);
        assert!(
            (snap.productivity_prior - 0.65).abs() < 1e-4,
            "expected Afternoon default 0.65, got {}",
            snap.productivity_prior
        );
    }
}
