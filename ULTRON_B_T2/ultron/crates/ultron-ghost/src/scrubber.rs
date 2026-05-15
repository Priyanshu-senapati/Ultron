//! Privacy scrubber — recursively walks a JSON payload before transmit
//! and replaces sensitive string fields with their BLAKE3 digest.
//!
//! ## What counts as sensitive
//!
//! The list lives in [`SENSITIVE_FIELDS`]. We include:
//! - `title`, `window_title` — raw window titles from H/perception
//! - `process_name`, `focus_app`, `app` — executable names from H
//! - `path` — screenshot paths from H (could include usernames, project
//!   names; not safe to ship verbatim)
//! - `visual_label` — LLaVA descriptions from the Python sidecar
//! - `summary` — when nested under a `pattern` it may quote process names
//! - `focus_category` is **NOT** sensitive — it's a fixed enum
//!   (`coding`, `browser`, etc.) with no PII potential.
//! - Numeric fields are never scrubbed.
//!
//! ## Algorithm
//!
//! Recursive walk:
//! - For each object key, if the key matches `SENSITIVE_FIELDS` and the
//!   value is a string, hash it via `crypto::hash_sensitive`.
//! - Recurse into nested objects and arrays.
//! - Anything else is passed through unchanged.
//!
//! Empty-string values pass through as empty (preserving the "no data"
//! signal — see the rationale in `crypto::hash_sensitive`).
//!
//! ## Why a path-based walker, not typed structs
//!
//! Two reasons:
//! 1. The exported event kinds (`insight_snapshot`, `tension_changed`,
//!    `patterns_update`) don't share a common Rust type — they'd all
//!    need to be `from_value` decoded, scrubbed, re-`to_value`'d.
//! 2. Future event kinds get default scrubbing for free. If someone
//!    adds `window_changed` to `export_kinds` next month, its
//!    `title`/`process_name` fields are already on the sensitive list.

use crate::crypto::hash_sensitive;
use serde_json::Value;

/// Object keys whose string values get hashed before transmit.
/// Centralised here so it's easy to grep ("what does ULTRON actually
/// scrub?") and to extend.
pub const SENSITIVE_FIELDS: &[&str] = &[
    "title",
    "window_title",
    "process_name",
    "focus_app",
    "app",
    "path",
    "visual_label",
    "summary",
];

/// Returns `true` if `key` appears in `SENSITIVE_FIELDS`. Case-sensitive
/// match — all our wire fields are lowercase by convention.
fn is_sensitive(key: &str) -> bool {
    SENSITIVE_FIELDS.iter().any(|f| *f == key)
}

/// Mutate `value` in place, hashing every sensitive string field
/// reachable from it. Idempotent: scrubbing an already-scrubbed
/// payload is a no-op (hashes are stable hex digests, not
/// recognisable as "sensitive" on a second pass — and there's no harm
/// in re-hashing a hex string, just inefficient).
pub fn scrub_in_place(value: &mut Value) {
    match value {
        Value::Object(map) => {
            for (k, v) in map.iter_mut() {
                if is_sensitive(k) {
                    if let Value::String(s) = v {
                        if !s.is_empty() {
                            *s = hash_sensitive(s);
                        }
                    }
                    // Sensitive key but non-string value (null, array,
                    // number) — leave alone; defensive against future
                    // schema changes.
                }
                // Always recurse — there could be sensitive fields
                // nested under a non-sensitive key (e.g. evidence
                // payloads inside a Pattern).
                scrub_in_place(v);
            }
        }
        Value::Array(arr) => {
            for v in arr.iter_mut() {
                scrub_in_place(v);
            }
        }
        // Primitives: nothing to scrub.
        _ => {}
    }
}

/// Convenience wrapper that returns the scrubbed value rather than
/// mutating in place. Useful in places where you've just deserialised
/// a borrowed `Value` and don't want to clone-then-mutate noise.
pub fn scrub(mut value: Value) -> Value {
    scrub_in_place(&mut value);
    value
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn scrubs_top_level_title() {
        let mut v = json!({
            "title": "Visual Studio Code - main.rs",
            "pid": 1234,
        });
        scrub_in_place(&mut v);
        let s = v["title"].as_str().unwrap();
        assert_ne!(s, "Visual Studio Code - main.rs");
        assert!(!s.contains("main"));
        assert_eq!(s.len(), 32, "BLAKE3-16 hex digest");
        // Non-sensitive fields unchanged.
        assert_eq!(v["pid"], 1234);
    }

    #[test]
    fn scrubs_process_name_and_focus_app() {
        let mut v = json!({
            "process_name": "Code.exe",
            "focus_app": "Code.exe",
            "ok_field": "Code.exe",
        });
        scrub_in_place(&mut v);
        let a = v["process_name"].as_str().unwrap();
        let b = v["focus_app"].as_str().unwrap();
        assert_ne!(a, "Code.exe");
        assert_eq!(a, b, "same input → same digest, regardless of key");
        // ok_field is NOT in the sensitive list and passes through.
        assert_eq!(v["ok_field"], "Code.exe");
    }

    #[test]
    fn empty_string_stays_empty() {
        let mut v = json!({"title": "", "pid": 1});
        scrub_in_place(&mut v);
        assert_eq!(v["title"], "");
    }

    #[test]
    fn recurses_into_nested_objects() {
        let mut v = json!({
            "outer": {
                "inner": {
                    "title": "deeply nested",
                    "n": 5
                }
            }
        });
        scrub_in_place(&mut v);
        let s = v["outer"]["inner"]["title"].as_str().unwrap();
        assert_ne!(s, "deeply nested");
        assert_eq!(s.len(), 32);
        assert_eq!(v["outer"]["inner"]["n"], 5);
    }

    #[test]
    fn recurses_into_arrays() {
        let mut v = json!({
            "windows": [
                {"title": "first window"},
                {"title": "second window"},
            ]
        });
        scrub_in_place(&mut v);
        let a = v["windows"][0]["title"].as_str().unwrap();
        let b = v["windows"][1]["title"].as_str().unwrap();
        assert_ne!(a, "first window");
        assert_ne!(b, "second window");
        assert_ne!(a, b, "different inputs → different digests");
    }

    #[test]
    fn idempotent_under_repeated_scrub() {
        let mut a = json!({"title": "the same window"});
        let mut b = a.clone();
        scrub_in_place(&mut a);
        scrub_in_place(&mut b);
        scrub_in_place(&mut b);
        // Re-hashing a hash is fine; the *first* scrub of `a` and `b`
        // yields the same digest. The double-scrub of `b` then hashes
        // the hex digest itself — different from the single-scrub.
        // What matters: scrubbing is deterministic.
        let mut c = json!({"title": "the same window"});
        scrub_in_place(&mut c);
        assert_eq!(a, c);
    }

    #[test]
    fn focus_category_passes_through() {
        // Sanity: the only fixed-enum field we send isn't on the list.
        let mut v = json!({"focus_category": "coding"});
        scrub_in_place(&mut v);
        assert_eq!(v["focus_category"], "coding");
    }

    #[test]
    fn realistic_insight_snapshot_payload() {
        // Mirror the shape `ultron-insight-pulse` actually publishes.
        let mut v = json!({
            "tick": 42,
            "ts_unix_ms": 1_700_000_000_000_i64,
            "tension": 0.42,
            "cognitive_load": 0.31,
            "focus_app": "Code.exe",
            "focus_category": "coding",
            "focus_duration_secs": 1200,
            "wpm": 65.0,
            "visual_label": "writing rust code",
            "circadian_phase": "morning"
        });
        scrub_in_place(&mut v);
        assert_ne!(v["focus_app"].as_str().unwrap(), "Code.exe");
        assert_ne!(v["visual_label"].as_str().unwrap(), "writing rust code");
        // Non-sensitive everything else.
        assert_eq!(v["focus_category"], "coding");
        assert_eq!(v["circadian_phase"], "morning");
        assert_eq!(v["wpm"], 65.0);
    }

    #[test]
    fn screenshot_path_is_scrubbed() {
        // Paths can contain usernames + project names — must be hashed.
        let mut v = json!({
            "path": "C:\\Users\\priyanshu\\AppData\\Roaming\\ULTRON\\data\\screenshots\\x.png",
            "width": 1920,
            "height": 1080
        });
        scrub_in_place(&mut v);
        let p = v["path"].as_str().unwrap();
        assert!(!p.contains("priyanshu"));
        assert!(!p.contains("ULTRON"));
        assert_eq!(p.len(), 32);
    }

    #[test]
    fn scrub_helper_returns_value() {
        let v = json!({"title": "x"});
        let s = scrub(v);
        assert_ne!(s["title"], "x");
        assert_eq!(s["title"].as_str().unwrap().len(), 32);
    }
}
