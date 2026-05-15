//! Pattern-detection payloads (Phase 1, Module D — Turn 3).
//!
//! Module D runs a small library of pattern detectors over the accumulated
//! snapshot history and publishes the results as a [`PatternsUpdate`] on a
//! slow cadence (default: every 60 minutes, or whenever the result set
//! changes materially).
//!
//! ## Stability promise
//!
//! `PatternKind` is a `String` rather than an enum on purpose. New detectors
//! land all the time during the early life of D, and forcing every
//! downstream consumer to know about every kind would make this brittle.
//! Consumers should match on known kinds and forward unknown ones unchanged.
//!
//! ## Wire format
//!
//! Published as an [`crate::events::UltronEvent::Custom`] with
//! `kind = "patterns_update"` and the serialised `PatternsUpdate` as the
//! payload. Same dispatch path as `productivity_prior_update`.

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Schema version of the current `PatternsUpdate` definition.
pub const PATTERNS_SCHEMA_VERSION: u32 = 1;

/// A single detected pattern. Pure data — no behaviour attached.
///
/// `summary` is short, human-readable, and safe to surface verbatim in a
/// notification ("you've had three high-load mornings this week").
/// `evidence` is a JSON object whose shape is detector-specific — for
/// example a "low-energy window" pattern might carry
/// `{"hour_local": 14, "mean_load": 0.72, "occurrences": 9}`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Pattern {
    /// Short identifier — see `PatternKind` for the canonical labels D
    /// emits today. Treat as opaque on the consumer side.
    pub kind: String,
    /// One-line human-readable summary. Always present, never empty.
    pub summary: String,
    /// Confidence in `[0.0, 1.0]`. Detectors set this from sample size
    /// + effect strength; tune downstream rendering by it.
    pub confidence: f32,
    /// Detector-specific structured payload. Free-form so detectors can
    /// evolve without bumping the schema.
    #[serde(default)]
    pub evidence: Value,
}

/// Canonical pattern-kind labels D emits in Phase 1. Future modules can
/// add their own kinds; consumers should treat the set as open.
pub mod kinds {
    /// Recurring time-of-day cognitive-load dip.
    /// `evidence`: `{"hour_local": 14, "mean_load": 0.71, "occurrences": 9}`.
    pub const LOW_ENERGY_WINDOW: &str = "low_energy_window";
    /// Recurring time-of-day calm/flow peak.
    /// `evidence`: `{"hour_local": 10, "mean_load": 0.22, "occurrences": 14}`.
    pub const HIGH_ENERGY_WINDOW: &str = "high_energy_window";
    /// A single app correlates with consistently elevated tension.
    /// `evidence`: `{"focus_category": "communication", "mean_tension": 0.62, "samples": 240}`.
    pub const APP_TENSION_CORRELATION: &str = "app_tension_correlation";
    /// A specific weekday is consistently below the weekly productivity
    /// mean. `evidence`: `{"weekday": "mon", "delta_vs_mean": -0.12}`.
    pub const WEEKDAY_DIP: &str = "weekday_dip";
}

/// Full pattern set published by D on each detection cycle. Replaces the
/// previous set entirely — consumers don't need to diff.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct PatternsUpdate {
    pub patterns: Vec<Pattern>,
    pub ts_unix_ms: i64,
    pub schema_version: u32,
}

impl PatternsUpdate {
    /// Wire `kind` value. Centralised so producers and consumers agree.
    pub const KIND: &'static str = "patterns_update";

    pub fn empty(now_ms: i64) -> Self {
        Self {
            patterns: Vec::new(),
            ts_unix_ms: now_ms,
            schema_version: PATTERNS_SCHEMA_VERSION,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn pattern_roundtrip() {
        let p = Pattern {
            kind: kinds::LOW_ENERGY_WINDOW.into(),
            summary: "consistent dip around 14:00".into(),
            confidence: 0.78,
            evidence: json!({"hour_local": 14, "mean_load": 0.71}),
        };
        let v = serde_json::to_string(&p).unwrap();
        let back: Pattern = serde_json::from_str(&v).unwrap();
        assert_eq!(p, back);
    }

    #[test]
    fn patterns_update_kind_is_snake_case() {
        assert_eq!(PatternsUpdate::KIND, "patterns_update");
    }

    #[test]
    fn empty_update_has_no_patterns() {
        let u = PatternsUpdate::empty(42);
        assert!(u.patterns.is_empty());
        assert_eq!(u.ts_unix_ms, 42);
        assert_eq!(u.schema_version, PATTERNS_SCHEMA_VERSION);
    }
}
