//! Insight Pulse payloads (Phase 1, Module O).
//!
//! The `InsightSnapshot` is the **primary consumable** for every downstream
//! ULTRON module — Memory Engine (D), LLM Client (C), Voice Engine (B),
//! HUD — all read it to adapt their behaviour to the user's cognitive
//! state. It is assembled by the `ultron-insight-pulse` Rust sidecar
//! every ~5 s and published as a `UltronEvent::Custom` with
//! `kind = "insight_snapshot"`.
//!
//! ## Stability promise
//!
//! Fields can be **added** in any minor revision; never **removed** or
//! retypecast without bumping the workspace version. Optional fields use
//! `Option<T>` or sentinel values (`0.0`, empty string) — never magic
//! numbers like `-1`.

use crate::perception::AppCategory;
use crate::tension::TensionBand;
use serde::{Deserialize, Serialize};

/// Coarse band over typing speed and storm-detection. Mostly informational
/// for the HUD; the raw `wpm` + `backspace_storm` are also exposed.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CadenceBand {
    /// `wpm < 5.0` — basically no typing.
    Idle,
    /// `wpm < 30.0`.
    Slow,
    /// `wpm < 80.0` — productive working speed.
    Normal,
    /// `wpm >= 80.0` **or** an active backspace storm. A storm overrides
    /// low WPM because it indicates frustrated activity, not calm.
    Frenetic,
}

impl CadenceBand {
    /// Classify a (wpm, storm) pair into a band. Centralised here so the
    /// Rust sidecar and any later consumer agree on thresholds.
    pub fn classify(wpm: f32, backspace_storm: bool) -> Self {
        if backspace_storm {
            return CadenceBand::Frenetic;
        }
        if wpm < 5.0 {
            CadenceBand::Idle
        } else if wpm < 30.0 {
            CadenceBand::Slow
        } else if wpm < 80.0 {
            CadenceBand::Normal
        } else {
            CadenceBand::Frenetic
        }
    }
}

/// Time-of-day partitioning used by Module O's circadian signal. Hours
/// are local-time hours (0..23) as returned by `chrono::Local::now()`.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CircadianPhase {
    /// 05:00 – 07:59
    EarlyMorning,
    /// 08:00 – 11:59
    Morning,
    /// 12:00 – 16:59
    Afternoon,
    /// 17:00 – 20:59
    Evening,
    /// 21:00 – 23:59
    Night,
    /// 00:00 – 04:59
    LateNight,
}

impl CircadianPhase {
    /// Classify by local-time hour (`0..=23`). Centralised so the Rust
    /// sidecar's tests and the production code don't drift.
    pub fn from_hour(hour: u32) -> Self {
        match hour {
            5..=7 => CircadianPhase::EarlyMorning,
            8..=11 => CircadianPhase::Morning,
            12..=16 => CircadianPhase::Afternoon,
            17..=20 => CircadianPhase::Evening,
            21..=23 => CircadianPhase::Night,
            _ => CircadianPhase::LateNight, // 0..=4
        }
    }

    /// Phase-1 clock-only productivity prior. Replaced by the Memory
    /// Engine (D) once it ships and starts feeding actual historical
    /// performance curves back into O.
    pub fn default_productivity_prior(self) -> f32 {
        match self {
            CircadianPhase::Morning => 0.85,
            CircadianPhase::Afternoon => 0.65,
            CircadianPhase::Evening => 0.55,
            CircadianPhase::EarlyMorning | CircadianPhase::Night => 0.40,
            CircadianPhase::LateNight => 0.25,
        }
    }
}

/// The full 5-signal snapshot Module O publishes every ~5 s. Field order
/// here mirrors the build prompt and is grouped by signal for readability
/// in the serialised JSON.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct InsightSnapshot {
    // ── Identity ────────────────────────────────────────────────────────
    /// Monotonically increasing counter since the O sidecar started.
    pub tick: u64,
    /// When this snapshot was assembled, in Unix epoch ms.
    pub ts_unix_ms: i64,

    // ── Signal 1: Tension ───────────────────────────────────────────────
    pub tension: f32,
    pub tension_band: TensionBand,
    /// `current - tension_60s_ago`. Positive = rising. `0.0` if history
    /// shorter than 60 s.
    pub tension_trend: f32,

    // ── Signal 2: Focus Context ─────────────────────────────────────────
    pub focus_app: String,
    pub focus_category: AppCategory,
    pub focus_duration_secs: u64,
    /// App switches per minute, averaged over the last 5 min.
    pub focus_switch_rate: f32,
    /// `clamp01(1.0 - switch_rate / 6.0)`. 6+ switches/min ⇒ 0.0.
    pub focus_score: f32,
    /// `true` when `focus_category == Coding` and `focus_duration_secs > 10800`.
    pub fatigue_flag: bool,

    // ── Signal 3: Input Cadence ─────────────────────────────────────────
    pub wpm: f32,
    pub wpm_slope_per_hour: f32,
    pub backspace_storm: bool,
    pub typing_rhythm_variance: f32,
    pub mouse_hesitation_score: f32,
    pub cadence_band: CadenceBand,

    // ── Signal 4: Visual Context ────────────────────────────────────────
    /// Latest LLaVA label. `None` when stale beyond
    /// `InsightConfig.visual_label_max_age_secs` (default 120 s).
    pub visual_label: Option<String>,
    /// Age of the label in seconds, regardless of whether we kept it. The
    /// HUD uses this to render confidence; consumers that drop stale
    /// labels can ignore it once `visual_label == None`.
    pub visual_label_age_secs: u32,

    // ── Signal 5: Circadian ─────────────────────────────────────────────
    pub circadian_phase: CircadianPhase,
    /// `[0.0, 1.0]`. Phase 1 uses clock-only defaults from
    /// `CircadianPhase::default_productivity_prior`. D will replace this
    /// later with a behavioural curve.
    pub productivity_prior: f32,

    // ── Derived composite ───────────────────────────────────────────────
    /// Weighted blend that downstream modules use as the single primary
    /// indicator of "how loaded is the user right now". See
    /// `compute_cognitive_load` for the exact formula.
    pub cognitive_load: f32,
}

/// Centralised formula for `cognitive_load`. Defined here (rather than
/// privately inside the sidecar) so other consumers can replicate it
/// without copy-paste drift.
///
/// ```text
/// cognitive_load = clamp01(
///     tension * 0.40
///   + (1.0 - focus_score) * 0.25
///   + typing_rhythm_variance * 0.15
///   + mouse_hesitation_score * 0.10
///   + (0.10 if backspace_storm else 0.00)
/// )
/// ```
#[inline]
pub fn compute_cognitive_load(
    tension: f32,
    focus_score: f32,
    typing_rhythm_variance: f32,
    mouse_hesitation_score: f32,
    backspace_storm: bool,
) -> f32 {
    crate::clamp01(
        tension * 0.40
            + (1.0 - focus_score) * 0.25
            + typing_rhythm_variance * 0.15
            + mouse_hesitation_score * 0.10
            + (if backspace_storm { 0.10 } else { 0.0 }),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cadence_band_thresholds() {
        assert_eq!(CadenceBand::classify(0.0, false), CadenceBand::Idle);
        assert_eq!(CadenceBand::classify(4.9, false), CadenceBand::Idle);
        assert_eq!(CadenceBand::classify(5.0, false), CadenceBand::Slow);
        assert_eq!(CadenceBand::classify(29.9, false), CadenceBand::Slow);
        assert_eq!(CadenceBand::classify(30.0, false), CadenceBand::Normal);
        assert_eq!(CadenceBand::classify(79.9, false), CadenceBand::Normal);
        assert_eq!(CadenceBand::classify(80.0, false), CadenceBand::Frenetic);
        // Storm overrides low WPM.
        assert_eq!(CadenceBand::classify(20.0, true), CadenceBand::Frenetic);
    }

    #[test]
    fn circadian_phase_from_hour() {
        assert_eq!(CircadianPhase::from_hour(0), CircadianPhase::LateNight);
        assert_eq!(CircadianPhase::from_hour(4), CircadianPhase::LateNight);
        assert_eq!(CircadianPhase::from_hour(5), CircadianPhase::EarlyMorning);
        assert_eq!(CircadianPhase::from_hour(8), CircadianPhase::Morning);
        assert_eq!(CircadianPhase::from_hour(12), CircadianPhase::Afternoon);
        assert_eq!(CircadianPhase::from_hour(17), CircadianPhase::Evening);
        assert_eq!(CircadianPhase::from_hour(21), CircadianPhase::Night);
        assert_eq!(CircadianPhase::from_hour(23), CircadianPhase::Night);
    }

    #[test]
    fn productivity_prior_defaults() {
        assert!((CircadianPhase::Morning.default_productivity_prior() - 0.85).abs() < 1e-6);
        assert!((CircadianPhase::LateNight.default_productivity_prior() - 0.25).abs() < 1e-6);
    }

    #[test]
    fn cognitive_load_zero_when_all_inputs_zero() {
        let cl = compute_cognitive_load(0.0, 1.0, 0.0, 0.0, false);
        assert!(cl.abs() < 1e-6, "cl was {cl}");
    }

    #[test]
    fn cognitive_load_known_value() {
        // tension=0.5, focus_score=0.4 (→ 0.6 lack), rhythm=0.2, hes=0.5, storm=true.
        // = 0.5*0.40 + 0.6*0.25 + 0.2*0.15 + 0.5*0.10 + 0.10
        // = 0.20 + 0.15 + 0.03 + 0.05 + 0.10 = 0.53
        let cl = compute_cognitive_load(0.5, 0.4, 0.2, 0.5, true);
        assert!((cl - 0.53).abs() < 1e-4, "cl was {cl}");
    }

    #[test]
    fn snapshot_roundtrip() {
        let s = InsightSnapshot {
            tick: 1,
            ts_unix_ms: 0,
            tension: 0.3,
            tension_band: TensionBand::Neutral,
            tension_trend: 0.0,
            focus_app: "Code.exe".into(),
            focus_category: AppCategory::Coding,
            focus_duration_secs: 60,
            focus_switch_rate: 0.5,
            focus_score: 0.9,
            fatigue_flag: false,
            wpm: 65.0,
            wpm_slope_per_hour: -3.2,
            backspace_storm: false,
            typing_rhythm_variance: 0.2,
            mouse_hesitation_score: 0.1,
            cadence_band: CadenceBand::Normal,
            visual_label: Some("writing rust code".into()),
            visual_label_age_secs: 12,
            circadian_phase: CircadianPhase::Afternoon,
            productivity_prior: 0.65,
            cognitive_load: 0.31,
        };
        let v = serde_json::to_string(&s).unwrap();
        let back: InsightSnapshot = serde_json::from_str(&v).unwrap();
        assert_eq!(s, back);
    }
}
