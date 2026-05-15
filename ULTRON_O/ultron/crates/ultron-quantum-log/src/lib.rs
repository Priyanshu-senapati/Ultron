//! # ULTRON Quantum Log
//!
//! Append-only, hash-chained, tamper-evident audit log.
//!
//! Every decision and action ULTRON makes is written here, **before** it
//! becomes effectful. Each entry's hash binds:
//!
//! ```text
//!   hash[i] = blake3( hash[i-1] || ts || kind || module || parent_id || payload )
//! ```
//!
//! So any single byte mutation downstream invalidates `verify_chain()`.
//! UPDATE and DELETE on the `entries` table are also blocked by SQL triggers.
//!
//! ## Concurrency
//!
//! All public methods are sync, protected by an internal mutex. Use the
//! `*_async` helpers from a Tokio runtime — they offload to `spawn_blocking`.

use blake3::Hasher;
use parking_lot::Mutex;
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum QLogError {
    #[error("sqlite: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("serde: {0}")]
    Serde(#[from] serde_json::Error),
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("integrity broken at id {id}: {reason}")]
    Integrity { id: i64, reason: String },
    #[error("join: {0}")]
    Join(#[from] tokio::task::JoinError),
}

/// Entry kinds. Add variants here as new modules come online.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum EntryKind {
    Boot,
    Shutdown,
    Event,
    Decision,
    Action,
    Reasoning,
    Error,
    Note,
    HeartbeatSnapshot,
    Tension,
    Input,
    Wire, // websocket events in / out
    /// Module O: a signal crossed threshold and was published.
    InsightFired,
    /// Module O: signal would have fired but was gated (rate limit,
    /// stale data, etc.). Logging suppressions is valuable for tuning.
    InsightSuppressed,
    /// Module O: routine tick summary. Sampled (every Nth tick) so the
    /// log doesn't drown in 1-Hz status updates.
    InsightTick,
}

impl EntryKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            EntryKind::Boot => "boot",
            EntryKind::Shutdown => "shutdown",
            EntryKind::Event => "event",
            EntryKind::Decision => "decision",
            EntryKind::Action => "action",
            EntryKind::Reasoning => "reasoning",
            EntryKind::Error => "error",
            EntryKind::Note => "note",
            EntryKind::HeartbeatSnapshot => "heartbeat_snapshot",
            EntryKind::Tension => "tension",
            EntryKind::Input => "input",
            EntryKind::Wire => "wire",
            EntryKind::InsightFired => "insight_fired",
            EntryKind::InsightSuppressed => "insight_suppressed",
            EntryKind::InsightTick => "insight_tick",
        }
    }
}

/// A new entry to be appended. `parent_id` chains a reasoning trace
/// (e.g. Decision -> Action) to its origin entry.
#[derive(Debug, Clone)]
pub struct NewEntry {
    pub kind: EntryKind,
    pub module: String,
    pub parent_id: Option<i64>,
    pub payload: serde_json::Value,
}

impl NewEntry {
    pub fn new(kind: EntryKind, module: impl Into<String>, payload: serde_json::Value) -> Self {
        Self {
            kind,
            module: module.into(),
            parent_id: None,
            payload,
        }
    }

    pub fn with_parent(mut self, parent: i64) -> Self {
        self.parent_id = Some(parent);
        self
    }
}

/// A persisted entry as returned to callers.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EntryRecord {
    pub id: i64,
    pub ts_unix_ms: i64,
    pub kind: String,
    pub module: String,
    pub parent_id: Option<i64>,
    pub payload: serde_json::Value,
    pub hash_hex: String,
    pub prev_hash_hex: String,
}

/// Handle to the audit log. Cheap to clone (shares an `Arc<Mutex<Connection>>`).
#[derive(Clone)]
pub struct QuantumLog {
    inner: Arc<Mutex<Connection>>,
    path: PathBuf,
}

impl QuantumLog {
    pub fn open<P: AsRef<Path>>(p: P) -> Result<Self, QLogError> {
        if let Some(parent) = p.as_ref().parent() {
            std::fs::create_dir_all(parent)?;
        }
        let conn = Connection::open(&p)?;
        // WAL gives us non-blocking reads while writes are in flight.
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "synchronous", "NORMAL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        conn.execute_batch(SCHEMA)?;
        Ok(Self {
            inner: Arc::new(Mutex::new(conn)),
            path: p.as_ref().to_path_buf(),
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Append a new entry. Returns the persisted record (incl. id and hash).
    pub fn append(&self, e: NewEntry) -> Result<EntryRecord, QLogError> {
        let mut conn = self.inner.lock();
        let tx = conn.transaction()?;

        let prev_hash: Vec<u8> = tx
            .query_row(
                "SELECT hash FROM entries ORDER BY id DESC LIMIT 1",
                [],
                |r| r.get(0),
            )
            .optional()?
            .unwrap_or_else(|| vec![0u8; 32]);

        let ts_unix_ms = chrono::Utc::now().timestamp_millis();
        let payload_json = serde_json::to_string(&e.payload)?;
        let kind_str = e.kind.as_str();

        let hash_bytes = compute_hash(
            &prev_hash,
            ts_unix_ms,
            kind_str,
            &e.module,
            e.parent_id,
            &payload_json,
        );

        tx.execute(
            "INSERT INTO entries
             (ts_unix_ms, kind, module, parent_id, payload, hash, prev_hash)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                ts_unix_ms,
                kind_str,
                e.module,
                e.parent_id,
                payload_json,
                hash_bytes,
                prev_hash,
            ],
        )?;
        let id = tx.last_insert_rowid();
        tx.commit()?;

        Ok(EntryRecord {
            id,
            ts_unix_ms,
            kind: kind_str.to_string(),
            module: e.module,
            parent_id: e.parent_id,
            payload: e.payload,
            hash_hex: hex::encode(&hash_bytes),
            prev_hash_hex: hex::encode(&prev_hash),
        })
    }

    /// Async sugar over [`Self::append`].
    pub async fn append_async(&self, e: NewEntry) -> Result<EntryRecord, QLogError> {
        let me = self.clone();
        tokio::task::spawn_blocking(move || me.append(e)).await?
    }

    /// Return the most recent `n` entries (id DESC).
    pub fn tail(&self, n: usize) -> Result<Vec<EntryRecord>, QLogError> {
        let conn = self.inner.lock();
        let mut stmt = conn.prepare(
            "SELECT id, ts_unix_ms, kind, module, parent_id, payload, hash, prev_hash
             FROM entries
             ORDER BY id DESC
             LIMIT ?1",
        )?;
        let rows = stmt.query_map(params![n as i64], row_to_record)?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(QLogError::from)
    }

    /// Walk all entries from the genesis and verify each row's hash + chain
    /// linkage. Returns the number of entries verified.
    pub fn verify_chain(&self) -> Result<usize, QLogError> {
        let conn = self.inner.lock();
        let mut stmt = conn.prepare(
            "SELECT id, ts_unix_ms, kind, module, parent_id, payload, hash, prev_hash
             FROM entries
             ORDER BY id ASC",
        )?;
        let mut rows = stmt.query([])?;
        let mut expected_prev: Vec<u8> = vec![0u8; 32];
        let mut count: usize = 0;

        while let Some(row) = rows.next()? {
            let id: i64 = row.get(0)?;
            let ts: i64 = row.get(1)?;
            let kind: String = row.get(2)?;
            let module: String = row.get(3)?;
            let parent_id: Option<i64> = row.get(4)?;
            let payload: String = row.get(5)?;
            let hash: Vec<u8> = row.get(6)?;
            let prev_hash: Vec<u8> = row.get(7)?;

            if prev_hash != expected_prev {
                return Err(QLogError::Integrity {
                    id,
                    reason: format!(
                        "prev_hash mismatch (got {}, expected {})",
                        hex::encode(&prev_hash),
                        hex::encode(&expected_prev),
                    ),
                });
            }

            let recomputed = compute_hash(&prev_hash, ts, &kind, &module, parent_id, &payload);
            if recomputed != hash {
                return Err(QLogError::Integrity {
                    id,
                    reason: "row hash does not match recomputed value".into(),
                });
            }

            expected_prev = hash;
            count += 1;
        }
        Ok(count)
    }

    pub fn count(&self) -> Result<i64, QLogError> {
        let conn = self.inner.lock();
        let n: i64 = conn.query_row("SELECT COUNT(*) FROM entries", [], |r| r.get(0))?;
        Ok(n)
    }
}

fn row_to_record(row: &rusqlite::Row<'_>) -> rusqlite::Result<EntryRecord> {
    let id: i64 = row.get(0)?;
    let ts: i64 = row.get(1)?;
    let kind: String = row.get(2)?;
    let module: String = row.get(3)?;
    let parent_id: Option<i64> = row.get(4)?;
    let payload_str: String = row.get(5)?;
    let hash: Vec<u8> = row.get(6)?;
    let prev_hash: Vec<u8> = row.get(7)?;
    let payload = serde_json::from_str(&payload_str).unwrap_or(serde_json::Value::Null);
    Ok(EntryRecord {
        id,
        ts_unix_ms: ts,
        kind,
        module,
        parent_id,
        payload,
        hash_hex: hex::encode(&hash),
        prev_hash_hex: hex::encode(&prev_hash),
    })
}

fn compute_hash(
    prev_hash: &[u8],
    ts_unix_ms: i64,
    kind: &str,
    module: &str,
    parent_id: Option<i64>,
    payload_json: &str,
) -> Vec<u8> {
    let mut h = Hasher::new();
    h.update(prev_hash);
    h.update(&ts_unix_ms.to_le_bytes());
    h.update(b"|");
    h.update(kind.as_bytes());
    h.update(b"|");
    h.update(module.as_bytes());
    h.update(b"|");
    if let Some(pid) = parent_id {
        h.update(&pid.to_le_bytes());
    } else {
        h.update(&(-1i64).to_le_bytes());
    }
    h.update(b"|");
    h.update(payload_json.as_bytes());
    h.finalize().as_bytes().to_vec()
}

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_unix_ms  INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    module      TEXT    NOT NULL,
    parent_id   INTEGER,
    payload     TEXT    NOT NULL,
    hash        BLOB    NOT NULL,
    prev_hash   BLOB    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_ts     ON entries(ts_unix_ms);
CREATE INDEX IF NOT EXISTS idx_entries_module ON entries(module);
CREATE INDEX IF NOT EXISTS idx_entries_kind   ON entries(kind);
CREATE INDEX IF NOT EXISTS idx_entries_parent ON entries(parent_id);

-- Append-only at the engine level.
CREATE TRIGGER IF NOT EXISTS entries_no_update
BEFORE UPDATE ON entries
BEGIN
    SELECT RAISE(ABORT, 'quantum_log is append-only (UPDATE forbidden)');
END;

CREATE TRIGGER IF NOT EXISTS entries_no_delete
BEFORE DELETE ON entries
BEGIN
    SELECT RAISE(ABORT, 'quantum_log is append-only (DELETE forbidden)');
END;
"#;

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn tmp_path(name: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("ultron_qlog_test_{name}_{}.db", std::process::id()));
        let _ = std::fs::remove_file(&p);
        p
    }

    #[test]
    fn append_and_tail() {
        let p = tmp_path("append");
        let q = QuantumLog::open(&p).unwrap();
        let r = q
            .append(NewEntry::new(
                EntryKind::Boot,
                "ultron-core",
                json!({"version": "0.1.0"}),
            ))
            .unwrap();
        assert_eq!(r.id, 1);
        assert_eq!(r.kind, "boot");
        assert_eq!(r.prev_hash_hex, hex::encode([0u8; 32]));

        let r2 = q
            .append(NewEntry::new(
                EntryKind::Note,
                "ultron-core",
                json!({"msg": "hello"}),
            ))
            .unwrap();
        assert_eq!(r2.id, 2);
        assert_eq!(r2.prev_hash_hex, r.hash_hex);

        let tail = q.tail(10).unwrap();
        assert_eq!(tail.len(), 2);
        assert_eq!(tail[0].id, 2); // tail() is DESC
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn verify_clean_chain() {
        let p = tmp_path("verify");
        let q = QuantumLog::open(&p).unwrap();
        for i in 0..50 {
            q.append(NewEntry::new(
                EntryKind::Note,
                "test",
                json!({ "i": i }),
            ))
            .unwrap();
        }
        assert_eq!(q.verify_chain().unwrap(), 50);
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn append_only_blocks_update_and_delete() {
        let p = tmp_path("immutable");
        let q = QuantumLog::open(&p).unwrap();
        q.append(NewEntry::new(EntryKind::Note, "test", json!({"x":1})))
            .unwrap();
        let conn = q.inner.lock();
        let upd = conn.execute("UPDATE entries SET payload='hacked' WHERE id=1", []);
        let del = conn.execute("DELETE FROM entries WHERE id=1", []);
        assert!(upd.is_err());
        assert!(del.is_err());
        drop(conn);
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn insight_entry_kinds_serialise() {
        // Fix 5 — Module O kinds must round-trip through the log.
        let p = tmp_path("insight_kinds");
        let q = QuantumLog::open(&p).unwrap();
        q.append(NewEntry::new(EntryKind::InsightFired, "o", json!({"x":1}))).unwrap();
        q.append(NewEntry::new(EntryKind::InsightSuppressed, "o", json!({"r":"stale"}))).unwrap();
        q.append(NewEntry::new(EntryKind::InsightTick, "o", json!({"tick":1}))).unwrap();
        assert_eq!(q.verify_chain().unwrap(), 3);
        assert_eq!(EntryKind::InsightFired.as_str(), "insight_fired");
        assert_eq!(EntryKind::InsightSuppressed.as_str(), "insight_suppressed");
        assert_eq!(EntryKind::InsightTick.as_str(), "insight_tick");
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn tampered_payload_breaks_chain() {
        let p = tmp_path("tamper");
        let q = QuantumLog::open(&p).unwrap();
        for i in 0..3 {
            q.append(NewEntry::new(EntryKind::Note, "t", json!({ "i": i })))
                .unwrap();
        }
        // Bypass triggers via a new connection — simulate a forensic mutation.
        // We can't just disable triggers from the same conn; drop the trigger
        // first using DROP TRIGGER (allowed because triggers protect entries,
        // not themselves).
        {
            let conn = q.inner.lock();
            conn.execute_batch(
                "DROP TRIGGER entries_no_update;
                 UPDATE entries SET payload='{\"i\":99}' WHERE id=2;",
            )
            .unwrap();
        }
        let err = q.verify_chain().unwrap_err();
        assert!(matches!(err, QLogError::Integrity { id: 2, .. }));
        let _ = std::fs::remove_file(&p);
    }

    #[tokio::test]
    async fn append_async_works() {
        let p = tmp_path("async");
        let q = QuantumLog::open(&p).unwrap();
        let r = q
            .append_async(NewEntry::new(EntryKind::Boot, "test", json!({})))
            .await
            .unwrap();
        assert_eq!(r.id, 1);
        let _ = std::fs::remove_file(&p);
    }
}
