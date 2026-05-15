//! SQLite persistence layer for Module D — Memory Engine.
//!
//! Owns its own database file (`%APPDATA%\ULTRON\data\memory.db`),
//! independent of the Quantum Log. Three tables in Turn 1:
//!
//! - `insight_snapshots` — every `InsightSnapshot` we receive, decomposed
//!   into queryable columns plus a `raw_snapshot` JSON column so future
//!   schema additions don't require backfilling.
//! - `visual_labels` — every `visual_label` event, joined with snapshots
//!   on `screenshot_ts` when Turn 2's learning needs visual context.
//! - `productivity_priors` — the learned per-hour curve. Empty until
//!   Turn 2 populates it; the table exists now so the schema is stable
//!   across upgrades.
//!
//! ## Concurrency
//!
//! All access goes through a single `Connection` behind a
//! `parking_lot::Mutex`. SQLite's WAL mode handles read-while-write, but
//! our access pattern is "one writer, one reader" so simple serialisation
//! is plenty. Mutex hold times are short — never across an `.await`.
//!
//! ## Migrations
//!
//! `user_version` PRAGMA is the source of truth for schema version. On
//! open we migrate forward step-by-step. Each migration is idempotent on
//! its target version.

use anyhow::{Context, Result};
use parking_lot::Mutex;
use rusqlite::{params, Connection, OptionalExtension};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tracing::{debug, info};
use ultron_types::InsightSnapshot;

const CURRENT_SCHEMA_VERSION: i32 = 2;

/// Shared, cloneable handle to the memory store. Holds an `Arc<Mutex<...>>`
/// over the connection, so cloning is cheap.
#[derive(Clone)]
pub struct MemoryStore {
    inner: Arc<Mutex<Connection>>,
    path: PathBuf,
}

impl MemoryStore {
    /// Open (or create) the memory database at `path`. Runs any pending
    /// migrations. Idempotent — calling repeatedly on the same path is
    /// safe.
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("create memory dir {}", parent.display()))?;
        }
        let conn = Connection::open(path)
            .with_context(|| format!("open memory db {}", path.display()))?;

        // WAL mode tolerates concurrent readers better; busy_timeout
        // saves us from spurious "database is locked" errors when the
        // future Module D query API kicks in.
        conn.pragma_update(None, "journal_mode", "WAL")
            .context("set WAL journal mode")?;
        conn.pragma_update(None, "synchronous", "NORMAL")
            .context("set synchronous NORMAL")?;
        conn.busy_timeout(std::time::Duration::from_secs(5))
            .context("set busy_timeout")?;

        let store = Self {
            inner: Arc::new(Mutex::new(conn)),
            path: path.to_path_buf(),
        };
        store.migrate().context("run migrations")?;
        Ok(store)
    }

    /// Path the store is backed by. Useful for logging.
    pub fn path(&self) -> &Path {
        &self.path
    }

    // -------------------------------------------------------------------
    // Migrations
    // -------------------------------------------------------------------

    fn migrate(&self) -> Result<()> {
        let conn = self.inner.lock();
        let current: i32 = conn
            .pragma_query_value(None, "user_version", |r| r.get(0))
            .context("read user_version")?;
        if current == CURRENT_SCHEMA_VERSION {
            debug!(version = current, "memory db schema is up to date");
            return Ok(());
        }
        info!(
            from = current,
            to = CURRENT_SCHEMA_VERSION,
            "migrating memory db"
        );
        // Step-by-step migrations. Each block migrates one version forward.
        if current < 1 {
            migrate_to_v1(&conn).context("migrate to v1")?;
        }
        if current < 2 {
            migrate_to_v2(&conn).context("migrate to v2")?;
        }
        conn.pragma_update(None, "user_version", CURRENT_SCHEMA_VERSION)
            .context("write user_version")?;
        Ok(())
    }

    // -------------------------------------------------------------------
    // Writes
    // -------------------------------------------------------------------

    /// Persist one [`InsightSnapshot`]. The full snapshot is round-tripped
    /// into the `raw_snapshot` column so future analyses can recover
    /// fields not yet promoted to dedicated columns.
    pub fn insert_snapshot(&self, snap: &InsightSnapshot) -> Result<i64> {
        let raw = serde_json::to_string(snap).context("serialise snapshot")?;
        let conn = self.inner.lock();
        conn.execute(
            "INSERT INTO insight_snapshots (
                ts_unix_ms, tick, tension, cognitive_load, focus_app,
                focus_category, focus_duration_secs, focus_score, wpm,
                wpm_slope_per_hour, backspace_storm, typing_rhythm_variance,
                mouse_hesitation_score, cadence_band, visual_label,
                visual_label_age_secs, circadian_phase, productivity_prior,
                fatigue_flag, raw_snapshot
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13,
                ?14, ?15, ?16, ?17, ?18, ?19, ?20
            )",
            params![
                snap.ts_unix_ms,
                snap.tick as i64,
                snap.tension as f64,
                snap.cognitive_load as f64,
                &snap.focus_app,
                snap.focus_category.as_str(),
                snap.focus_duration_secs as i64,
                snap.focus_score as f64,
                snap.wpm as f64,
                snap.wpm_slope_per_hour as f64,
                snap.backspace_storm as i32,
                snap.typing_rhythm_variance as f64,
                snap.mouse_hesitation_score as f64,
                cadence_band_str(&snap.cadence_band),
                snap.visual_label.as_deref(),
                snap.visual_label_age_secs as i64,
                circadian_phase_str(&snap.circadian_phase),
                snap.productivity_prior as f64,
                snap.fatigue_flag as i32,
                raw,
            ],
        )
        .context("insert snapshot")?;
        Ok(conn.last_insert_rowid())
    }

    /// Persist one observed `visual_label` event. We store these
    /// separately from snapshots because labels arrive on their own
    /// schedule (asynchronously from the LLaVA sidecar) — they can be
    /// joined back to snapshots later.
    pub fn insert_visual_label(
        &self,
        label: &str,
        screenshot_ts_ms: Option<i64>,
        ts_unix_ms: i64,
    ) -> Result<i64> {
        let conn = self.inner.lock();
        conn.execute(
            "INSERT INTO visual_labels (ts_unix_ms, screenshot_ts_ms, label) VALUES (?1, ?2, ?3)",
            params![ts_unix_ms, screenshot_ts_ms, label],
        )
        .context("insert visual label")?;
        Ok(conn.last_insert_rowid())
    }

    // -------------------------------------------------------------------
    // Reads
    // -------------------------------------------------------------------

    /// Total snapshots stored. Used for /health endpoints, smoke tests.
    pub fn snapshot_count(&self) -> Result<i64> {
        let conn = self.inner.lock();
        let n: i64 = conn
            .query_row("SELECT COUNT(*) FROM insight_snapshots", [], |r| r.get(0))
            .context("count snapshots")?;
        Ok(n)
    }

    /// Most recent snapshot timestamp, if any. Lets the engine pick up
    /// after restart without re-ingesting from the beginning.
    pub fn latest_snapshot_ts_ms(&self) -> Result<Option<i64>> {
        let conn = self.inner.lock();
        let ts: Option<i64> = conn
            .query_row(
                "SELECT MAX(ts_unix_ms) FROM insight_snapshots",
                [],
                |r| r.get(0),
            )
            .optional()
            .context("query latest ts")?
            .flatten();
        Ok(ts)
    }

    /// Snapshots strictly newer than `since_ms`, in ascending time order.
    /// Used by Turn 2's learning loop and Turn 3's pattern detection.
    /// Returns `(ts_unix_ms, focus_category, cognitive_load, wpm, tension)`
    /// — the columns Turn 2 needs without pulling the full row.
    pub fn snapshots_since(
        &self,
        since_ms: i64,
        limit: usize,
    ) -> Result<Vec<SnapshotRow>> {
        let conn = self.inner.lock();
        let mut stmt = conn
            .prepare(
                "SELECT ts_unix_ms, focus_category, cognitive_load, wpm, tension
                 FROM insight_snapshots
                 WHERE ts_unix_ms > ?1
                 ORDER BY ts_unix_ms ASC
                 LIMIT ?2",
            )
            .context("prepare snapshots_since")?;
        let rows = stmt
            .query_map(params![since_ms, limit as i64], |r| {
                Ok(SnapshotRow {
                    ts_unix_ms: r.get(0)?,
                    focus_category: r.get(1)?,
                    cognitive_load: r.get::<_, f64>(2)? as f32,
                    wpm: r.get::<_, f64>(3)? as f32,
                    tension: r.get::<_, f64>(4)? as f32,
                })
            })
            .context("query snapshots_since")?
            .collect::<rusqlite::Result<Vec<_>>>()
            .context("collect snapshot rows")?;
        Ok(rows)
    }

    /// Convenience for Turn 2: count rows per (local) hour-of-day. Pure
    /// SQL aggregation — much faster than pulling rows into Rust just to
    /// histogram them.
    ///
    /// Note: this uses `strftime` with a `localtime` modifier, so the
    /// result respects the user's local timezone. SQLite's local-time
    /// handling reads the OS TZ at query time.
    pub fn snapshots_per_local_hour(&self, since_ms: i64) -> Result<[u32; 24]> {
        let conn = self.inner.lock();
        let mut stmt = conn
            .prepare(
                "SELECT CAST(strftime('%H', ts_unix_ms / 1000, 'unixepoch', 'localtime') AS INTEGER) AS hour,
                        COUNT(*) AS n
                 FROM insight_snapshots
                 WHERE ts_unix_ms >= ?1
                 GROUP BY hour",
            )
            .context("prepare hourly count")?;
        let mut counts = [0u32; 24];
        let iter = stmt
            .query_map(params![since_ms], |r| {
                let h: i64 = r.get(0)?;
                let n: i64 = r.get(1)?;
                Ok((h, n))
            })
            .context("query hourly count")?;
        for row in iter {
            let (h, n) = row.context("read hourly count row")?;
            if (0..24).contains(&h) {
                counts[h as usize] = n.max(0) as u32;
            }
        }
        Ok(counts)
    }

    // -------------------------------------------------------------------
    // Productivity priors — Turn 2
    // -------------------------------------------------------------------

    /// Read every row from `productivity_priors`. Vec is naturally
    /// sorted by hour because that's the primary key, but callers
    /// shouldn't rely on it — use `.iter().find(|p| p.hour == h)`.
    pub fn read_all_priors(&self) -> Result<Vec<crate::learning::StoredPrior>> {
        let conn = self.inner.lock();
        let mut stmt = conn
            .prepare(
                "SELECT hour, base_prior, sample_count, last_updated_ms
                 FROM productivity_priors
                 ORDER BY hour",
            )
            .context("prepare read_all_priors")?;
        let rows = stmt
            .query_map([], |r| {
                Ok(crate::learning::StoredPrior {
                    hour: r.get::<_, i64>(0)? as u32,
                    base_prior: r.get::<_, f64>(1)? as f32,
                    sample_count: r.get::<_, i64>(2)? as u32,
                    last_updated_ms: r.get(3)?,
                })
            })
            .context("query read_all_priors")?
            .collect::<rusqlite::Result<Vec<_>>>()
            .context("collect priors")?;
        Ok(rows)
    }

    /// Insert or update the prior for `hour` (0..=23). Day-of-week
    /// modifiers stay at their defaults (Turn 3 will populate them).
    pub fn upsert_prior(
        &self,
        hour: u32,
        base_prior: f32,
        sample_count: u32,
        now_ms: i64,
    ) -> Result<()> {
        if hour > 23 {
            anyhow::bail!("hour out of range: {hour}");
        }
        let conn = self.inner.lock();
        conn.execute(
            "INSERT INTO productivity_priors
                 (hour, base_prior, sample_count, last_updated_ms)
             VALUES (?1, ?2, ?3, ?4)
             ON CONFLICT(hour) DO UPDATE SET
                 base_prior = excluded.base_prior,
                 sample_count = excluded.sample_count,
                 last_updated_ms = excluded.last_updated_ms",
            params![hour as i64, base_prior as f64, sample_count as i64, now_ms],
        )
        .context("upsert prior")?;
        Ok(())
    }

    /// Update the day-of-week modifier for one hour. `weekday` is 0–6
    /// (Mon=0..Sun=6, matching `chrono::Weekday::num_days_from_monday`).
    /// Idempotent; the row for `hour` must already exist (created by
    /// the first `upsert_prior` call).
    ///
    /// The modifier is a *delta* from `base_prior`, expressed in the
    /// same units (`[-1.0, 1.0]`). Consumers compute the final per-day
    /// prior as `clamp01(base_prior + day_modifier)`.
    pub fn upsert_day_modifier(&self, hour: u32, weekday: u32, modifier: f32) -> Result<()> {
        if hour > 23 {
            anyhow::bail!("hour out of range: {hour}");
        }
        let col = match weekday {
            0 => "day_mod_mon",
            1 => "day_mod_tue",
            2 => "day_mod_wed",
            3 => "day_mod_thu",
            4 => "day_mod_fri",
            5 => "day_mod_sat",
            6 => "day_mod_sun",
            _ => anyhow::bail!("weekday out of range: {weekday}"),
        };
        // `col` comes from a fixed match, so it's safe to interpolate.
        let sql = format!(
            "UPDATE productivity_priors SET {col} = ?1 WHERE hour = ?2"
        );
        let conn = self.inner.lock();
        let n = conn
            .execute(&sql, params![modifier as f64, hour as i64])
            .context("upsert day modifier")?;
        if n == 0 {
            // No row yet — create one with zero base prior so the
            // modifier sticks. The next learning cycle will overwrite
            // the base when enough samples accrue.
            conn.execute(
                "INSERT OR IGNORE INTO productivity_priors
                     (hour, base_prior, sample_count, last_updated_ms)
                 VALUES (?1, 0.0, 0, 0)",
                params![hour as i64],
            )
            .context("seed prior row for day modifier")?;
            conn.execute(&sql, params![modifier as f64, hour as i64])
                .context("retry upsert day modifier")?;
        }
        Ok(())
    }

    /// Append one detected pattern. Patterns are append-only.
    pub fn insert_pattern(
        &self,
        ts_unix_ms: i64,
        kind: &str,
        summary: &str,
        confidence: f32,
        evidence: &serde_json::Value,
    ) -> Result<i64> {
        let conn = self.inner.lock();
        let evidence_json = serde_json::to_string(evidence).unwrap_or_else(|_| "null".into());
        conn.execute(
            "INSERT INTO detected_patterns (ts_unix_ms, kind, summary, confidence, evidence)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![ts_unix_ms, kind, summary, confidence as f64, evidence_json],
        )
        .context("insert pattern")?;
        Ok(conn.last_insert_rowid())
    }

    /// Read every pattern emitted in the last `since_ms..` window.
    pub fn read_recent_patterns(&self, since_ms: i64) -> Result<Vec<StoredPattern>> {
        let conn = self.inner.lock();
        let mut stmt = conn
            .prepare(
                "SELECT id, ts_unix_ms, kind, summary, confidence, evidence
                 FROM detected_patterns
                 WHERE ts_unix_ms >= ?1
                 ORDER BY ts_unix_ms DESC",
            )
            .context("prepare read_recent_patterns")?;
        let rows = stmt
            .query_map(params![since_ms], |r| {
                let ev_text: String = r.get(5)?;
                Ok(StoredPattern {
                    id: r.get(0)?,
                    ts_unix_ms: r.get(1)?,
                    kind: r.get(2)?,
                    summary: r.get(3)?,
                    confidence: r.get::<_, f64>(4)? as f32,
                    evidence_json: ev_text,
                })
            })
            .context("query read_recent_patterns")?
            .collect::<rusqlite::Result<Vec<_>>>()
            .context("collect patterns")?;
        Ok(rows)
    }
}

/// One row from `snapshots_since`. Lightweight; tuned for batch reads
/// during learning.
#[derive(Debug, Clone)]
pub struct SnapshotRow {
    pub ts_unix_ms: i64,
    pub focus_category: String,
    pub cognitive_load: f32,
    pub wpm: f32,
    pub tension: f32,
}

/// One row from `read_recent_patterns`. `evidence_json` is kept as a
/// `String` so consumers that don't care about it can skip the parse.
#[derive(Debug, Clone)]
pub struct StoredPattern {
    pub id: i64,
    pub ts_unix_ms: i64,
    pub kind: String,
    pub summary: String,
    pub confidence: f32,
    pub evidence_json: String,
}

// =====================================================================
// Migration steps
// =====================================================================

fn migrate_to_v1(conn: &Connection) -> Result<()> {
    // Single statement-per-call to keep error messages precise.
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS insight_snapshots (
            id                        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_unix_ms                INTEGER NOT NULL,
            tick                      INTEGER NOT NULL,
            tension                   REAL    NOT NULL,
            cognitive_load            REAL    NOT NULL,
            focus_app                 TEXT    NOT NULL,
            focus_category            TEXT    NOT NULL,
            focus_duration_secs       INTEGER NOT NULL,
            focus_score               REAL    NOT NULL,
            wpm                       REAL    NOT NULL,
            wpm_slope_per_hour        REAL    NOT NULL,
            backspace_storm           INTEGER NOT NULL,
            typing_rhythm_variance    REAL    NOT NULL,
            mouse_hesitation_score    REAL    NOT NULL,
            cadence_band              TEXT    NOT NULL,
            visual_label              TEXT,
            visual_label_age_secs     INTEGER NOT NULL,
            circadian_phase           TEXT    NOT NULL,
            productivity_prior        REAL    NOT NULL,
            fatigue_flag              INTEGER NOT NULL,
            raw_snapshot              TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_ts        ON insight_snapshots(ts_unix_ms);
        CREATE INDEX IF NOT EXISTS idx_snapshots_category  ON insight_snapshots(focus_category);
        CREATE INDEX IF NOT EXISTS idx_snapshots_band      ON insight_snapshots(cadence_band);

        CREATE TABLE IF NOT EXISTS visual_labels (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_unix_ms        INTEGER NOT NULL,
            screenshot_ts_ms  INTEGER,
            label             TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_labels_ts ON visual_labels(ts_unix_ms);
        CREATE INDEX IF NOT EXISTS idx_labels_screenshot_ts ON visual_labels(screenshot_ts_ms);

        CREATE TABLE IF NOT EXISTS productivity_priors (
            hour             INTEGER PRIMARY KEY,   -- 0..=23 (local time)
            base_prior       REAL    NOT NULL,
            sample_count     INTEGER NOT NULL,
            day_mod_mon      REAL    DEFAULT 0.0,
            day_mod_tue      REAL    DEFAULT 0.0,
            day_mod_wed      REAL    DEFAULT 0.0,
            day_mod_thu      REAL    DEFAULT 0.0,
            day_mod_fri      REAL    DEFAULT 0.0,
            day_mod_sat      REAL    DEFAULT 0.0,
            day_mod_sun      REAL    DEFAULT 0.0,
            last_updated_ms  INTEGER NOT NULL
        );
        "#,
    )
    .context("execute v1 schema batch")?;
    Ok(())
}

fn migrate_to_v2(conn: &Connection) -> Result<()> {
    // Turn 3 — pattern detection. Patterns are append-only audit trail
    // and observability; the wire `PatternsUpdate` is the source of
    // truth for live consumers, this table is for "show me last week's
    // detected dips".
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS detected_patterns (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_unix_ms   INTEGER NOT NULL,
            kind         TEXT    NOT NULL,
            summary      TEXT    NOT NULL,
            confidence   REAL    NOT NULL,
            evidence     TEXT    NOT NULL   -- JSON
        );
        CREATE INDEX IF NOT EXISTS idx_patterns_ts   ON detected_patterns(ts_unix_ms);
        CREATE INDEX IF NOT EXISTS idx_patterns_kind ON detected_patterns(kind);
        "#,
    )
    .context("execute v2 schema batch")?;
    Ok(())
}

// =====================================================================
// Helpers
// =====================================================================

fn cadence_band_str(b: &ultron_types::CadenceBand) -> &'static str {
    match b {
        ultron_types::CadenceBand::Idle => "idle",
        ultron_types::CadenceBand::Slow => "slow",
        ultron_types::CadenceBand::Normal => "normal",
        ultron_types::CadenceBand::Frenetic => "frenetic",
    }
}

fn circadian_phase_str(p: &ultron_types::CircadianPhase) -> &'static str {
    match p {
        ultron_types::CircadianPhase::EarlyMorning => "early_morning",
        ultron_types::CircadianPhase::Morning => "morning",
        ultron_types::CircadianPhase::Afternoon => "afternoon",
        ultron_types::CircadianPhase::Evening => "evening",
        ultron_types::CircadianPhase::Night => "night",
        ultron_types::CircadianPhase::LateNight => "late_night",
    }
}

// =====================================================================
// Tests
// =====================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use ultron_types::{AppCategory, CadenceBand, CircadianPhase, TensionBand};

    fn tmp_db() -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("ultron_mem_test_{}.db", std::process::id()));
        p.push(format!("d_{}.db", chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)));
        // Each test gets its own file under a per-process subdir.
        if let Some(parent) = p.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let _ = std::fs::remove_file(&p);
        p
    }

    fn sample_snapshot(ts_unix_ms: i64, tick: u64) -> InsightSnapshot {
        InsightSnapshot {
            tick,
            ts_unix_ms,
            tension: 0.42,
            tension_band: TensionBand::Loaded,
            tension_trend: 0.05,
            focus_app: "Code.exe".into(),
            focus_category: AppCategory::Coding,
            focus_duration_secs: 600,
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
            cognitive_load: 0.3,
        }
    }

    #[test]
    fn open_creates_schema_and_is_idempotent() {
        let p = tmp_db();
        let s1 = MemoryStore::open(&p).unwrap();
        assert_eq!(s1.snapshot_count().unwrap(), 0);
        // Re-opening the same file must not error and must preserve state.
        drop(s1);
        let s2 = MemoryStore::open(&p).unwrap();
        assert_eq!(s2.snapshot_count().unwrap(), 0);
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn insert_and_count_snapshot() {
        let p = tmp_db();
        let s = MemoryStore::open(&p).unwrap();
        let id = s.insert_snapshot(&sample_snapshot(1_700_000_000_000, 1)).unwrap();
        assert!(id > 0);
        assert_eq!(s.snapshot_count().unwrap(), 1);
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn latest_ts_tracks_inserts() {
        let p = tmp_db();
        let s = MemoryStore::open(&p).unwrap();
        assert_eq!(s.latest_snapshot_ts_ms().unwrap(), None);
        s.insert_snapshot(&sample_snapshot(100, 1)).unwrap();
        s.insert_snapshot(&sample_snapshot(500, 2)).unwrap();
        s.insert_snapshot(&sample_snapshot(300, 3)).unwrap();
        assert_eq!(s.latest_snapshot_ts_ms().unwrap(), Some(500));
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn snapshots_since_returns_ascending() {
        let p = tmp_db();
        let s = MemoryStore::open(&p).unwrap();
        for (i, ts) in [500, 100, 300, 200, 400].iter().enumerate() {
            s.insert_snapshot(&sample_snapshot(*ts, i as u64)).unwrap();
        }
        let rows = s.snapshots_since(150, 100).unwrap();
        let tss: Vec<i64> = rows.iter().map(|r| r.ts_unix_ms).collect();
        assert_eq!(tss, vec![200, 300, 400, 500]);
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn visual_label_insert() {
        let p = tmp_db();
        let s = MemoryStore::open(&p).unwrap();
        let id = s
            .insert_visual_label("writing python code", Some(1_700_000_000_000), 1_700_000_000_500)
            .unwrap();
        assert!(id > 0);
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn snapshots_per_local_hour_buckets_correctly() {
        let p = tmp_db();
        let s = MemoryStore::open(&p).unwrap();
        // Insert a few snapshots at distinct UTC times. We only check the
        // total — the per-hour bucketing depends on the test box's TZ.
        for i in 0..5 {
            s.insert_snapshot(&sample_snapshot(
                1_700_000_000_000 + (i as i64) * 3_600_000,
                i,
            ))
            .unwrap();
        }
        let counts = s.snapshots_per_local_hour(0).unwrap();
        let total: u32 = counts.iter().sum();
        assert_eq!(total, 5);
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn re_open_preserves_data() {
        let p = tmp_db();
        {
            let s = MemoryStore::open(&p).unwrap();
            s.insert_snapshot(&sample_snapshot(1, 1)).unwrap();
            s.insert_snapshot(&sample_snapshot(2, 2)).unwrap();
        }
        let s = MemoryStore::open(&p).unwrap();
        assert_eq!(s.snapshot_count().unwrap(), 2);
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn upsert_and_read_priors() {
        let p = tmp_db();
        let s = MemoryStore::open(&p).unwrap();
        // Empty initially.
        let priors = s.read_all_priors().unwrap();
        assert!(priors.is_empty());

        // Insert two hours.
        s.upsert_prior(9, 0.85, 120, 1_000).unwrap();
        s.upsert_prior(14, 0.55, 80, 1_001).unwrap();
        let priors = s.read_all_priors().unwrap();
        assert_eq!(priors.len(), 2);

        // Re-upsert hour 9 with different values — must update, not duplicate.
        s.upsert_prior(9, 0.92, 150, 1_002).unwrap();
        let priors = s.read_all_priors().unwrap();
        assert_eq!(priors.len(), 2);
        let h9 = priors.iter().find(|p| p.hour == 9).unwrap();
        assert!((h9.base_prior - 0.92).abs() < 1e-4);
        assert_eq!(h9.sample_count, 150);
        assert_eq!(h9.last_updated_ms, 1_002);

        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn upsert_prior_rejects_out_of_range_hour() {
        let p = tmp_db();
        let s = MemoryStore::open(&p).unwrap();
        let r = s.upsert_prior(24, 0.5, 10, 0);
        assert!(r.is_err());
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn migrate_to_v2_creates_patterns_table() {
        // Open creates schema. Inserting into detected_patterns
        // succeeds ⇒ migration ran.
        let p = tmp_db();
        let s = MemoryStore::open(&p).unwrap();
        let id = s
            .insert_pattern(
                1_000,
                "low_energy_window",
                "dip around 14:00",
                0.7,
                &serde_json::json!({"hour_local": 14}),
            )
            .unwrap();
        assert!(id > 0);
        let recent = s.read_recent_patterns(0).unwrap();
        assert_eq!(recent.len(), 1);
        assert_eq!(recent[0].kind, "low_energy_window");
        assert!((recent[0].confidence - 0.7).abs() < 1e-4);
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn read_recent_patterns_filters_by_ts() {
        let p = tmp_db();
        let s = MemoryStore::open(&p).unwrap();
        for ts in [100i64, 500, 1_000, 2_000] {
            s.insert_pattern(ts, "k", "s", 0.5, &serde_json::json!({})).unwrap();
        }
        let recent = s.read_recent_patterns(600).unwrap();
        assert_eq!(recent.len(), 2, "should match ts=1000 and 2000");
        // DESC order — newest first.
        assert!(recent[0].ts_unix_ms > recent[1].ts_unix_ms);
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn upsert_day_modifier_seeds_row_if_missing() {
        let p = tmp_db();
        let s = MemoryStore::open(&p).unwrap();
        // No prior row exists yet — upserting a modifier should seed one.
        s.upsert_day_modifier(10, 0, -0.12).unwrap();
        let priors = s.read_all_priors().unwrap();
        // Sanity: hour 10 row exists, even if base_prior is 0.
        let h10 = priors.iter().find(|p| p.hour == 10);
        assert!(h10.is_some(), "row for hour 10 should exist");
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn upsert_day_modifier_rejects_bad_weekday() {
        let p = tmp_db();
        let s = MemoryStore::open(&p).unwrap();
        assert!(s.upsert_day_modifier(10, 7, 0.0).is_err());
        let _ = std::fs::remove_file(&p);
    }
}
