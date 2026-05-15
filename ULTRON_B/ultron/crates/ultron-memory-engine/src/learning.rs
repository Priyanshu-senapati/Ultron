//! Productivity prior learning + (Turn 3) pattern detection.
//!
//! ## Turn 2 — what this file does
//!
//! - Pulls the last [`DEFAULT_HISTORY_DAYS`] of snapshots from SQLite.
//! - Buckets them by **local** hour-of-day (0–23).
//! - For each hour, computes a productivity score from `cognitive_load`
//!   and `wpm`:
//!     `score = 0.65 * (1 - mean(cognitive_load))  +  0.35 * normalised_wpm`
//!   where `normalised_wpm = clamp01(mean(wpm) / WPM_REFERENCE)`.
//! - EWMA-smooths each hour's score against the previous published value
//!   (read back from the `productivity_priors` SQLite table) so the curve
//!   doesn't oscillate from one publish to the next.
//! - Persists the smoothed result back to SQLite.
//! - Returns a `ProductivityPriorUpdate` ready to publish.
//!
//! ## Why this design
//!
//! - **Local hour**, not UTC. The user's circadian rhythm is local; if
//!   Priyanshu travels and the OS TZ changes, the curve naturally
//!   shifts with it.
//! - **Combine cognitive_load and WPM**. Either alone is misleading.
//!   Low cognitive_load could mean "idle and bored", not "in flow".
//!   WPM disambiguates: low load + high WPM = productive flow.
//! - **EWMA against the persisted value**, not against the previous
//!   in-memory tick. This way a restart doesn't reset the curve to zero
//!   — D picks up exactly where it left off.
//! - **`min_samples` gating**. An hour with only 3 observations
//!   shouldn't override the circadian default. Hours below threshold
//!   are published as `None` (consumer keeps using its default).
//!
//! ## Turn 3 will add
//!
//! - Day-of-week modifiers (the `day_mod_*` columns already exist in
//!   the schema, they're just zero for now)
//! - Pattern detection (recurring low-energy windows, app+tension
//!   correlations) — likely in a sibling module imported here

use crate::store::{MemoryStore, SnapshotRow};
use anyhow::Result;
use chrono::{DateTime, Local, TimeZone, Timelike, Utc};
use tracing::{debug, info};
use ultron_types::{clamp01, ProductivityPriorUpdate};

/// Minimum number of snapshots in an hour before we trust the learned
/// value enough to override O's circadian default.
pub const DEFAULT_MIN_SAMPLES_PER_HOUR: u32 = 30;

/// Window of history we consider. 14 days is enough to smooth daily
/// noise without letting month-old behaviour dominate the curve.
pub const DEFAULT_HISTORY_DAYS: i64 = 14;

/// WPM value that maps to a normalised "1.0" productivity contribution.
/// Calibrated for typical knowledge-work typing (60 WPM ≈ productive).
const WPM_REFERENCE: f32 = 60.0;

/// EWMA smoothing factor. New observation gets this weight; old value
/// gets `1 - α`. With α = 0.2 and a 10-minute publish interval the
/// half-life is about 30 minutes of *publish time* — gentle enough to
/// be stable, fast enough to track real change.
const EWMA_ALPHA: f32 = 0.20;

/// Per-hour aggregate computed before smoothing.
#[derive(Debug, Clone)]
struct HourBucket {
    mean_cognitive_load: f32,
    mean_wpm: f32,
    count: u32,
}

/// Persisted prior for one hour, read out of SQLite.
#[derive(Debug, Clone, Copy)]
pub struct StoredPrior {
    pub hour: u32,
    pub base_prior: f32,
    pub sample_count: u32,
    pub last_updated_ms: i64,
}

#[derive(Clone)]
pub struct ProductivityLearner {
    store: MemoryStore,
    min_samples_per_hour: u32,
    history_days: i64,
}

impl ProductivityLearner {
    pub fn new(store: MemoryStore) -> Self {
        Self {
            store,
            min_samples_per_hour: DEFAULT_MIN_SAMPLES_PER_HOUR,
            history_days: DEFAULT_HISTORY_DAYS,
        }
    }

    /// Builder-style override — tests pin these so they can exercise
    /// the gating logic without inserting 30 sample rows.
    pub fn with_min_samples(mut self, n: u32) -> Self {
        self.min_samples_per_hour = n;
        self
    }

    pub fn with_history_days(mut self, d: i64) -> Self {
        self.history_days = d;
        self
    }

    /// Compute the current best-estimate productivity prior. Reads
    /// recent snapshots, aggregates by local hour, blends with the
    /// previously-persisted prior via EWMA, writes back, returns the
    /// payload ready to publish.
    ///
    /// Safe to call from a blocking task — does its own SQLite I/O.
    pub fn compute(&self, now_ms: i64) -> Result<ProductivityPriorUpdate> {
        let since_ms = now_ms - self.history_days * 86_400_000;
        let rows = self.store.snapshots_since(since_ms, 1_000_000)?;
        if rows.is_empty() {
            debug!("no history yet; returning empty prior");
            return Ok(ProductivityPriorUpdate::empty(now_ms));
        }

        let buckets = bucket_by_local_hour(&rows);
        let previous = self.store.read_all_priors()?;

        let mut out = ProductivityPriorUpdate::empty(now_ms);

        for hour in 0..24u32 {
            let Some(bucket) = buckets[hour as usize].as_ref() else {
                continue;
            };
            if bucket.count < self.min_samples_per_hour {
                // Hour exists but doesn't have enough samples yet —
                // record sample count for transparency but emit no
                // override; consumer keeps its default.
                out.sample_counts[hour as usize] = bucket.count;
                continue;
            }

            let raw_score = score_from_bucket(bucket);

            // EWMA-blend against the previously-persisted prior. First
            // time we see an hour, prev is None ⇒ no smoothing.
            let prev = previous.iter().find(|p| p.hour == hour).copied();
            let smoothed = match prev {
                Some(p) => clamp01(EWMA_ALPHA * raw_score + (1.0 - EWMA_ALPHA) * p.base_prior),
                None => clamp01(raw_score),
            };

            out.priors[hour as usize] = Some(smoothed);
            out.sample_counts[hour as usize] = bucket.count;

            // Persist back so the next call sees this as `previous`.
            self.store
                .upsert_prior(hour, smoothed, bucket.count, now_ms)?;
        }

        let learned = out.priors.iter().filter(|p| p.is_some()).count();
        info!(
            history_days = self.history_days,
            history_rows = rows.len(),
            learned_hours = learned,
            "productivity prior recomputed"
        );

        Ok(out)
    }
}

// =====================================================================
// Pure helpers (testable without a database)
// =====================================================================

fn bucket_by_local_hour(rows: &[SnapshotRow]) -> [Option<HourBucket>; 24] {
    let mut sum_cl = [0.0f64; 24];
    let mut sum_wpm = [0.0f64; 24];
    let mut counts = [0u32; 24];

    for row in rows {
        let hour = local_hour_for(row.ts_unix_ms);
        sum_cl[hour as usize] += row.cognitive_load as f64;
        sum_wpm[hour as usize] += row.wpm as f64;
        counts[hour as usize] += 1;
    }

    let mut out: [Option<HourBucket>; 24] = Default::default();
    for h in 0..24usize {
        if counts[h] == 0 {
            continue;
        }
        let n = counts[h] as f64;
        out[h] = Some(HourBucket {
            mean_cognitive_load: (sum_cl[h] / n) as f32,
            mean_wpm: (sum_wpm[h] / n) as f32,
            count: counts[h],
        });
    }
    out
}

fn score_from_bucket(bucket: &HourBucket) -> f32 {
    let load_term = clamp01(1.0 - bucket.mean_cognitive_load);
    let wpm_term = clamp01(bucket.mean_wpm / WPM_REFERENCE);
    clamp01(0.65 * load_term + 0.35 * wpm_term)
}

fn local_hour_for(ts_unix_ms: i64) -> u32 {
    match Utc.timestamp_millis_opt(ts_unix_ms) {
        chrono::LocalResult::Single(dt) => {
            let local: DateTime<Local> = dt.with_timezone(&Local);
            local.hour()
        }
        _ => 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::MemoryStore;
    use std::path::PathBuf;
    use ultron_types::{
        AppCategory, CadenceBand, CircadianPhase, InsightSnapshot, TensionBand,
    };

    fn tmp_store() -> (MemoryStore, PathBuf) {
        let mut p = std::env::temp_dir();
        p.push(format!(
            "ultron_mem_learn_{}_{}.db",
            std::process::id(),
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        ));
        let _ = std::fs::remove_file(&p);
        (MemoryStore::open(&p).unwrap(), p)
    }

    fn snap(ts_unix_ms: i64, tick: u64, cl: f32, wpm: f32) -> InsightSnapshot {
        InsightSnapshot {
            tick,
            ts_unix_ms,
            tension: cl,
            tension_band: TensionBand::Neutral,
            tension_trend: 0.0,
            focus_app: "x".into(),
            focus_category: AppCategory::Coding,
            focus_duration_secs: 0,
            focus_switch_rate: 0.0,
            focus_score: 0.0,
            fatigue_flag: false,
            wpm,
            wpm_slope_per_hour: 0.0,
            backspace_storm: false,
            typing_rhythm_variance: 0.0,
            mouse_hesitation_score: 0.0,
            cadence_band: CadenceBand::Normal,
            visual_label: None,
            visual_label_age_secs: 0,
            circadian_phase: CircadianPhase::Afternoon,
            productivity_prior: 0.0,
            cognitive_load: cl,
        }
    }

    fn row(ts_unix_ms: i64, cl: f32, wpm: f32) -> SnapshotRow {
        SnapshotRow {
            ts_unix_ms,
            focus_category: "coding".into(),
            cognitive_load: cl,
            wpm,
            tension: 0.0,
        }
    }

    #[test]
    fn score_from_bucket_known_value() {
        // cl=0.2, wpm=60 ⇒ load_term=0.8, wpm_term=1.0
        // score = 0.65*0.8 + 0.35*1.0 = 0.52 + 0.35 = 0.87
        let b = HourBucket {
            mean_cognitive_load: 0.2,
            mean_wpm: 60.0,
            count: 50,
        };
        let s = score_from_bucket(&b);
        assert!((s - 0.87).abs() < 1e-3, "score = {s}");
    }

    #[test]
    fn score_caps_at_one_for_extreme_wpm() {
        let b = HourBucket {
            mean_cognitive_load: 0.0,
            mean_wpm: 500.0,
            count: 50,
        };
        let s = score_from_bucket(&b);
        assert!(s <= 1.0 + 1e-6);
        assert!((s - 1.0).abs() < 1e-3);
    }

    #[test]
    fn score_zero_when_overloaded_and_silent() {
        let b = HourBucket {
            mean_cognitive_load: 1.0,
            mean_wpm: 0.0,
            count: 50,
        };
        let s = score_from_bucket(&b);
        assert_eq!(s, 0.0);
    }

    #[test]
    fn bucket_by_local_hour_aggregates_correctly() {
        let now = chrono::Utc::now().timestamp_millis();
        let rows = vec![row(now, 0.2, 60.0), row(now, 0.4, 80.0), row(now, 0.6, 40.0)];
        let b = bucket_by_local_hour(&rows);
        let nonempty: Vec<_> = b.iter().filter_map(|x| x.as_ref()).collect();
        assert_eq!(nonempty.len(), 1, "all rows should land in one hour");
        let hour = nonempty[0];
        assert_eq!(hour.count, 3);
        assert!((hour.mean_cognitive_load - 0.4).abs() < 1e-4);
        assert!((hour.mean_wpm - 60.0).abs() < 1e-4);
    }

    #[test]
    fn compute_empty_history_returns_empty() {
        let (store, p) = tmp_store();
        let learner = ProductivityLearner::new(store);
        let out = learner.compute(1_700_000_000_000).unwrap();
        assert!(!out.has_any_data());
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn compute_respects_min_samples_gate() {
        let (store, p) = tmp_store();
        let now = chrono::Utc::now().timestamp_millis();
        // Insert only 5 snapshots — below the default 30 threshold.
        for i in 0..5 {
            store.insert_snapshot(&snap(now - i * 1_000, i as u64, 0.2, 60.0)).unwrap();
        }
        let learner = ProductivityLearner::new(store.clone());
        let out = learner.compute(now).unwrap();
        let any_learned = out.priors.iter().any(|p| p.is_some());
        let any_count = out.sample_counts.iter().any(|c| *c > 0);
        assert!(!any_learned, "should not have learned anything yet");
        assert!(any_count, "sample counts should still be visible");
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn compute_publishes_when_threshold_reached() {
        let (store, p) = tmp_store();
        let now = chrono::Utc::now().timestamp_millis();
        for i in 0..60 {
            store
                .insert_snapshot(&snap(now - i * 1_000, i as u64, 0.2, 60.0))
                .unwrap();
        }
        let learner = ProductivityLearner::new(store.clone()).with_min_samples(30);
        let out = learner.compute(now).unwrap();
        assert!(out.has_any_data());
        let learned: Vec<f32> = out.priors.iter().filter_map(|p| *p).collect();
        assert_eq!(learned.len(), 1);
        let v = learned[0];
        assert!(v > 0.8 && v < 0.95, "prior was {v}");
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn compute_persists_priors_for_next_call() {
        let (store, p) = tmp_store();
        let now = chrono::Utc::now().timestamp_millis();
        for i in 0..60 {
            store
                .insert_snapshot(&snap(now - i * 1_000, i as u64, 0.2, 60.0))
                .unwrap();
        }
        let learner = ProductivityLearner::new(store.clone()).with_min_samples(30);
        let _ = learner.compute(now).unwrap();
        let stored_after_first = store.read_all_priors().unwrap();
        assert!(!stored_after_first.is_empty());
        let _ = learner.compute(now).unwrap();
        let stored_after_second = store.read_all_priors().unwrap();
        assert_eq!(stored_after_first.len(), stored_after_second.len());
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn compute_smooths_via_ewma() {
        let (store, p) = tmp_store();
        let now = chrono::Utc::now().timestamp_millis();
        for i in 0..60 {
            store
                .insert_snapshot(&snap(now - i * 1_000, i as u64, 0.1, 80.0))
                .unwrap();
        }
        let learner = ProductivityLearner::new(store.clone()).with_min_samples(30);
        let first = learner.compute(now).unwrap();
        let first_val = first.priors.iter().filter_map(|p| *p).next().unwrap();
        assert!(first_val > 0.8, "first val was {first_val}");

        // Now add 60 "low productivity" rows for the same hour.
        for i in 60..120 {
            store
                .insert_snapshot(&snap(now - i * 1_000, i as u64, 0.9, 5.0))
                .unwrap();
        }
        let second = learner.compute(now).unwrap();
        let second_val = second.priors.iter().filter_map(|p| *p).next().unwrap();

        // EWMA: second_val should be strictly between the previous and
        // the raw new mean, and closer to the previous (α=0.2).
        assert!(second_val < first_val, "should drift toward lower raw");
        assert!(
            second_val > 0.5,
            "should not have collapsed to the raw value: {second_val}"
        );
        let _ = std::fs::remove_file(&p);
    }
}
