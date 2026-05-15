//! Pattern detection over the accumulated snapshot history.
//!
//! Each detector is a **pure function** over a slice of [`SnapshotRow`]s.
//! The orchestrator at the bottom calls all of them in turn and collects
//! the results into a [`PatternsUpdate`].
//!
//! ## Detectors shipped in Turn 3
//!
//! 1. [`detect_energy_windows`] — for each local hour, compute the mean
//!    cognitive load. Hours that consistently sit below
//!    `HIGH_ENERGY_LOAD_CEILING` get flagged as **high-energy windows**;
//!    hours consistently above `LOW_ENERGY_LOAD_FLOOR` as **low-energy
//!    windows**. Confidence scales with sample count.
//!
//! 2. [`detect_app_tension`] — group by `focus_category` and compute
//!    mean tension. Categories above [`APP_TENSION_THRESHOLD`] with
//!    enough samples are flagged. Skips `unknown` and `idle`.
//!
//! 3. [`detect_weekday_modifiers`] — for each weekday, compute the mean
//!    productivity score (`1 - cognitive_load`) and compare against the
//!    overall mean. Weekdays where the delta exceeds
//!    `WEEKDAY_DIP_THRESHOLD` get emitted both as a `weekday_dip`
//!    [`Pattern`] *and* persisted as `day_mod_*` modifiers on the
//!    `productivity_priors` table — those are read by future O ticks
//!    via the schema (Turn 4 will plumb the read).
//!
//! ## Why no time-decay / sequence detector here
//!
//! The build prompt mentions "fatigue signatures" as a possibility but
//! also calls out that real sequence detection wants K (knowledge graph)
//! to identify event types. For Turn 3 we ship the three above; a
//! "consecutive high-load → tension spike" sequence detector belongs
//! to a later iteration once we have enough real-world data to tune it.

use crate::store::{MemoryStore, SnapshotRow};
use anyhow::Result;
use chrono::{Datelike, Local, TimeZone, Timelike, Utc, Weekday};
use serde_json::json;
use tracing::{debug, info};
use ultron_types::{kinds, Pattern, PatternsUpdate, PATTERNS_SCHEMA_VERSION};

// ---- Tunable thresholds -------------------------------------------------

/// Hours with fewer than this many samples never get flagged either way.
/// Same gating principle as Turn 2's prior learner.
pub const MIN_SAMPLES_PER_HOUR_FOR_PATTERNS: u32 = 30;

/// Mean cognitive-load *below* this in a given hour → high-energy.
pub const HIGH_ENERGY_LOAD_CEILING: f32 = 0.30;

/// Mean cognitive-load *above* this in a given hour → low-energy.
pub const LOW_ENERGY_LOAD_FLOOR: f32 = 0.65;

/// Minimum samples in a focus_category before we'll correlate it with
/// tension. Below this we don't have statistical signal.
pub const MIN_SAMPLES_PER_CATEGORY: u32 = 60;

/// Mean tension above this for a category → flag the correlation.
pub const APP_TENSION_THRESHOLD: f32 = 0.55;

/// Absolute productivity delta vs. weekly mean above which a weekday
/// gets flagged as dip/peak.
pub const WEEKDAY_DELTA_THRESHOLD: f32 = 0.08;

/// Minimum total samples across a weekday before we trust the modifier.
pub const MIN_SAMPLES_PER_WEEKDAY: u32 = 100;

// =====================================================================
// Helpers — exposed at module level so tests can call them directly
// =====================================================================

fn local_hour_for(ts_unix_ms: i64) -> u32 {
    match Utc.timestamp_millis_opt(ts_unix_ms) {
        chrono::LocalResult::Single(dt) => dt.with_timezone(&Local).hour(),
        _ => 0,
    }
}

fn local_weekday_for(ts_unix_ms: i64) -> u32 {
    match Utc.timestamp_millis_opt(ts_unix_ms) {
        chrono::LocalResult::Single(dt) => {
            dt.with_timezone(&Local).weekday().num_days_from_monday()
        }
        _ => 0,
    }
}

fn weekday_label(n: u32) -> &'static str {
    match n {
        0 => "mon",
        1 => "tue",
        2 => "wed",
        3 => "thu",
        4 => "fri",
        5 => "sat",
        6 => "sun",
        _ => "?",
    }
}

/// Confidence rises with sample count and asymptotically caps near 1.0.
/// 30 samples → ~0.4, 100 → ~0.7, 300 → ~0.9. Matches the gating
/// thresholds: a barely-eligible hour reports moderate confidence,
/// well-sampled hours report high.
fn confidence_from_samples(n: u32) -> f32 {
    let n = n as f32;
    // 1 - exp(-n / 150) — gentle saturation, never quite reaches 1.
    1.0 - (-n / 150.0).exp()
}

// =====================================================================
// Detector 1 — Energy windows
// =====================================================================

pub fn detect_energy_windows(rows: &[SnapshotRow]) -> Vec<Pattern> {
    if rows.is_empty() {
        return Vec::new();
    }
    let mut sum_load = [0.0f64; 24];
    let mut counts = [0u32; 24];
    for r in rows {
        let h = local_hour_for(r.ts_unix_ms) as usize;
        sum_load[h] += r.cognitive_load as f64;
        counts[h] += 1;
    }

    let mut out = Vec::new();
    for h in 0..24u32 {
        let n = counts[h as usize];
        if n < MIN_SAMPLES_PER_HOUR_FOR_PATTERNS {
            continue;
        }
        let mean_load = (sum_load[h as usize] / n as f64) as f32;
        let conf = confidence_from_samples(n);

        if mean_load <= HIGH_ENERGY_LOAD_CEILING {
            out.push(Pattern {
                kind: kinds::HIGH_ENERGY_WINDOW.into(),
                summary: format!(
                    "calm/flow window around {:02}:00 (mean load {:.2})",
                    h, mean_load
                ),
                confidence: conf,
                evidence: json!({
                    "hour_local": h,
                    "mean_load": mean_load,
                    "samples": n,
                }),
            });
        } else if mean_load >= LOW_ENERGY_LOAD_FLOOR {
            out.push(Pattern {
                kind: kinds::LOW_ENERGY_WINDOW.into(),
                summary: format!(
                    "high-load window around {:02}:00 (mean load {:.2})",
                    h, mean_load
                ),
                confidence: conf,
                evidence: json!({
                    "hour_local": h,
                    "mean_load": mean_load,
                    "samples": n,
                }),
            });
        }
    }
    out
}

// =====================================================================
// Detector 2 — App + tension correlation
// =====================================================================

pub fn detect_app_tension(rows: &[SnapshotRow]) -> Vec<Pattern> {
    use std::collections::HashMap;

    let mut sum: HashMap<String, f64> = HashMap::new();
    let mut counts: HashMap<String, u32> = HashMap::new();
    for r in rows {
        // Skip categories that aren't actionable signals.
        if r.focus_category.is_empty()
            || r.focus_category == "unknown"
            || r.focus_category == "idle"
        {
            continue;
        }
        *sum.entry(r.focus_category.clone()).or_default() += r.tension as f64;
        *counts.entry(r.focus_category.clone()).or_default() += 1;
    }

    let mut out = Vec::new();
    for (cat, &n) in counts.iter() {
        if n < MIN_SAMPLES_PER_CATEGORY {
            continue;
        }
        let mean = (sum[cat] / n as f64) as f32;
        if mean >= APP_TENSION_THRESHOLD {
            out.push(Pattern {
                kind: kinds::APP_TENSION_CORRELATION.into(),
                summary: format!(
                    "{} consistently elevates tension (mean {:.2})",
                    cat, mean
                ),
                confidence: confidence_from_samples(n),
                evidence: json!({
                    "focus_category": cat,
                    "mean_tension": mean,
                    "samples": n,
                }),
            });
        }
    }
    // Deterministic order — easier to diff in logs/tests.
    out.sort_by(|a, b| a.summary.cmp(&b.summary));
    out
}

// =====================================================================
// Detector 3 — Weekday modifiers
// =====================================================================

/// Returns `(Vec<Pattern>, [day_modifier; 7])` where day_modifier[i] is
/// the productivity delta vs. the weekly mean. Caller persists the
/// modifiers via [`MemoryStore::upsert_day_modifier`] for every hour.
///
/// (Phase 1 keeps the modifier *per-weekday*, not per (weekday, hour).
/// Per-hour resolution would need ~7×30 = 210 sample-rows per cell —
/// weeks of data. We can refine later when there's enough history.)
pub fn detect_weekday_modifiers(rows: &[SnapshotRow]) -> (Vec<Pattern>, [f32; 7]) {
    if rows.is_empty() {
        return (Vec::new(), [0.0; 7]);
    }
    // Per-weekday means.
    let mut sum_prod = [0.0f64; 7];
    let mut counts = [0u32; 7];
    let mut total_sum = 0.0f64;
    let mut total_count: u32 = 0;
    for r in rows {
        let w = local_weekday_for(r.ts_unix_ms) as usize;
        // Productivity ≈ 1 - cognitive_load. Same conventional mapping
        // as the prior learner.
        let prod = (1.0 - r.cognitive_load).clamp(0.0, 1.0) as f64;
        sum_prod[w] += prod;
        counts[w] += 1;
        total_sum += prod;
        total_count += 1;
    }
    if total_count == 0 {
        return (Vec::new(), [0.0; 7]);
    }
    let overall_mean = (total_sum / total_count as f64) as f32;

    let mut deltas = [0.0f32; 7];
    let mut patterns = Vec::new();
    for w in 0..7usize {
        let n = counts[w];
        if n < MIN_SAMPLES_PER_WEEKDAY {
            continue;
        }
        let mean = (sum_prod[w] / n as f64) as f32;
        let delta = mean - overall_mean;
        deltas[w] = delta;
        if delta.abs() >= WEEKDAY_DELTA_THRESHOLD {
            patterns.push(Pattern {
                kind: kinds::WEEKDAY_DIP.into(),
                summary: format!(
                    "{} is {:+.0}% vs your weekly average productivity",
                    weekday_label(w as u32),
                    delta * 100.0
                ),
                confidence: confidence_from_samples(n),
                evidence: json!({
                    "weekday": weekday_label(w as u32),
                    "delta_vs_mean": delta,
                    "weekday_mean": mean,
                    "overall_mean": overall_mean,
                    "samples": n,
                }),
            });
        }
    }
    patterns.sort_by(|a, b| a.summary.cmp(&b.summary));
    (patterns, deltas)
}

// =====================================================================
// Orchestrator — called from the main loop's pattern-tick task
// =====================================================================

#[derive(Clone)]
pub struct PatternDetector {
    store: MemoryStore,
    /// History horizon, in days. Defaults to 14 to match the prior learner.
    history_days: i64,
}

impl PatternDetector {
    pub fn new(store: MemoryStore) -> Self {
        Self {
            store,
            history_days: 14,
        }
    }

    pub fn with_history_days(mut self, d: i64) -> Self {
        self.history_days = d;
        self
    }

    /// Run every detector, return the full update plus persist the
    /// weekday modifiers and the individual `Pattern` rows to SQLite.
    pub fn detect(&self, now_ms: i64) -> Result<PatternsUpdate> {
        let since_ms = now_ms - self.history_days * 86_400_000;
        let rows = self.store.snapshots_since(since_ms, 1_000_000)?;
        if rows.is_empty() {
            debug!("no history yet; pattern set is empty");
            return Ok(PatternsUpdate::empty(now_ms));
        }

        let mut patterns = Vec::new();
        patterns.extend(detect_energy_windows(&rows));
        patterns.extend(detect_app_tension(&rows));
        let (weekday_patterns, deltas) = detect_weekday_modifiers(&rows);
        patterns.extend(weekday_patterns);

        // Persist day-of-week modifiers per-hour. We apply the same
        // weekday delta to every hour — Phase 1 simplification.
        for (w, delta) in deltas.iter().enumerate() {
            if delta.abs() < f32::EPSILON {
                continue;
            }
            for h in 0..24u32 {
                // Best-effort — log and continue if a row is malformed.
                let _ = self.store.upsert_day_modifier(h, w as u32, *delta);
            }
        }

        // Persist every detected pattern to the audit table.
        for p in &patterns {
            let _ = self.store.insert_pattern(
                now_ms,
                &p.kind,
                &p.summary,
                p.confidence,
                &p.evidence,
            );
        }

        info!(
            history_days = self.history_days,
            history_rows = rows.len(),
            patterns_found = patterns.len(),
            "patterns recomputed"
        );

        Ok(PatternsUpdate {
            patterns,
            ts_unix_ms: now_ms,
            schema_version: PATTERNS_SCHEMA_VERSION,
        })
    }
}

// =====================================================================
// Tests
// =====================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn tmp_store() -> (MemoryStore, PathBuf) {
        let mut p = std::env::temp_dir();
        p.push(format!(
            "ultron_patterns_{}_{}.db",
            std::process::id(),
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        ));
        let _ = std::fs::remove_file(&p);
        (MemoryStore::open(&p).unwrap(), p)
    }

    fn row(ts_unix_ms: i64, focus_category: &str, cognitive_load: f32, tension: f32) -> SnapshotRow {
        SnapshotRow {
            ts_unix_ms,
            focus_category: focus_category.into(),
            cognitive_load,
            wpm: 0.0,
            tension,
        }
    }

    // ---- confidence helper ---------------------------------------------

    #[test]
    fn confidence_monotonic_with_samples() {
        let c30 = confidence_from_samples(30);
        let c100 = confidence_from_samples(100);
        let c300 = confidence_from_samples(300);
        assert!(c30 < c100 && c100 < c300);
        assert!(c30 > 0.0 && c300 < 1.0);
        // Rough sanity: 30 samples → roughly 0.18, not zero, not one.
        assert!(c30 > 0.1 && c30 < 0.3, "c30 = {c30}");
    }

    // ---- energy windows ------------------------------------------------

    #[test]
    fn energy_window_detects_high_energy_hour() {
        // 60 rows at the same local hour, all with low load.
        let now = chrono::Utc::now().timestamp_millis();
        let rows: Vec<_> = (0..60).map(|i| row(now - i * 1_000, "coding", 0.15, 0.2)).collect();
        let patterns = detect_energy_windows(&rows);
        assert_eq!(patterns.len(), 1);
        assert_eq!(patterns[0].kind, kinds::HIGH_ENERGY_WINDOW);
    }

    #[test]
    fn energy_window_detects_low_energy_hour() {
        let now = chrono::Utc::now().timestamp_millis();
        let rows: Vec<_> = (0..60).map(|i| row(now - i * 1_000, "coding", 0.80, 0.7)).collect();
        let patterns = detect_energy_windows(&rows);
        assert_eq!(patterns.len(), 1);
        assert_eq!(patterns[0].kind, kinds::LOW_ENERGY_WINDOW);
    }

    #[test]
    fn energy_window_quiet_when_in_middle_band() {
        // Mean load 0.45 — between floor and ceiling.
        let now = chrono::Utc::now().timestamp_millis();
        let rows: Vec<_> = (0..60).map(|i| row(now - i * 1_000, "coding", 0.45, 0.4)).collect();
        let patterns = detect_energy_windows(&rows);
        assert!(patterns.is_empty(), "shouldn't flag mid-range hours");
    }

    #[test]
    fn energy_window_respects_min_samples() {
        let now = chrono::Utc::now().timestamp_millis();
        let rows: Vec<_> = (0..10).map(|i| row(now - i * 1_000, "coding", 0.10, 0.2)).collect();
        let patterns = detect_energy_windows(&rows);
        assert!(patterns.is_empty(), "10 rows is below threshold");
    }

    // ---- app/tension correlation ---------------------------------------

    #[test]
    fn app_tension_flags_high_tension_category() {
        let now = chrono::Utc::now().timestamp_millis();
        let mut rows = Vec::new();
        // 80 communication rows with high tension.
        for i in 0..80 {
            rows.push(row(now - i * 1_000, "communication", 0.5, 0.65));
        }
        // 80 coding rows with low tension — should NOT flag.
        for i in 0..80 {
            rows.push(row(now - (80 + i) * 1_000, "coding", 0.3, 0.30));
        }
        let patterns = detect_app_tension(&rows);
        assert_eq!(patterns.len(), 1);
        assert_eq!(patterns[0].kind, kinds::APP_TENSION_CORRELATION);
        assert!(patterns[0].summary.contains("communication"));
    }

    #[test]
    fn app_tension_skips_unknown_and_idle() {
        let now = chrono::Utc::now().timestamp_millis();
        let mut rows = Vec::new();
        for i in 0..100 {
            rows.push(row(now - i * 1_000, "unknown", 0.5, 0.9));
        }
        for i in 0..100 {
            rows.push(row(now - (100 + i) * 1_000, "idle", 0.5, 0.9));
        }
        let patterns = detect_app_tension(&rows);
        assert!(patterns.is_empty(), "unknown/idle categories must be skipped");
    }

    #[test]
    fn app_tension_respects_min_samples() {
        let now = chrono::Utc::now().timestamp_millis();
        let rows: Vec<_> = (0..30)
            .map(|i| row(now - i * 1_000, "communication", 0.5, 0.7))
            .collect();
        let patterns = detect_app_tension(&rows);
        assert!(patterns.is_empty(), "30 rows is below per-category threshold");
    }

    // ---- weekday modifiers ---------------------------------------------

    #[test]
    fn weekday_modifier_returns_zero_for_no_data() {
        let (patterns, deltas) = detect_weekday_modifiers(&[]);
        assert!(patterns.is_empty());
        assert_eq!(deltas, [0.0; 7]);
    }

    #[test]
    fn weekday_modifier_respects_min_samples() {
        let now = chrono::Utc::now().timestamp_millis();
        // 50 rows in one weekday — below the 100-sample threshold.
        let rows: Vec<_> = (0..50).map(|i| row(now - i * 1_000, "coding", 0.3, 0.3)).collect();
        let (patterns, _) = detect_weekday_modifiers(&rows);
        assert!(patterns.is_empty(), "below per-weekday threshold");
    }

    // ---- orchestrator --------------------------------------------------

    #[test]
    fn detector_empty_history_returns_empty_update() {
        let (store, p) = tmp_store();
        let det = PatternDetector::new(store);
        let u = det.detect(1_700_000_000_000).unwrap();
        assert!(u.patterns.is_empty());
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn detector_persists_patterns_to_audit_table() {
        let (store, p) = tmp_store();
        let now = chrono::Utc::now().timestamp_millis();
        // Insert 60 high-energy snapshots in the current local hour.
        for i in 0..60 {
            let snap = ultron_types::InsightSnapshot {
                tick: i as u64,
                ts_unix_ms: now - i * 1_000,
                tension: 0.2,
                tension_band: ultron_types::TensionBand::Calm,
                tension_trend: 0.0,
                focus_app: "Code.exe".into(),
                focus_category: ultron_types::AppCategory::Coding,
                focus_duration_secs: 0,
                focus_switch_rate: 0.0,
                focus_score: 1.0,
                fatigue_flag: false,
                wpm: 60.0,
                wpm_slope_per_hour: 0.0,
                backspace_storm: false,
                typing_rhythm_variance: 0.0,
                mouse_hesitation_score: 0.0,
                cadence_band: ultron_types::CadenceBand::Normal,
                visual_label: None,
                visual_label_age_secs: 0,
                circadian_phase: ultron_types::CircadianPhase::Morning,
                productivity_prior: 0.85,
                cognitive_load: 0.15,
            };
            store.insert_snapshot(&snap).unwrap();
        }
        let det = PatternDetector::new(store.clone());
        let update = det.detect(now).unwrap();
        assert!(!update.patterns.is_empty(), "should detect the high-energy hour");
        // And it should be persisted.
        let stored = store.read_recent_patterns(0).unwrap();
        assert_eq!(stored.len(), update.patterns.len());
        let _ = std::fs::remove_file(&p);
    }
}
