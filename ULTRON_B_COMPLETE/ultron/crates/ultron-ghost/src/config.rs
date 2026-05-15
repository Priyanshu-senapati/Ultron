//! Configuration loading for the ghost sidecar.
//!
//! The full ULTRON `config.toml` is owned by `ultron-core`. This module
//! only deserialises the slice it cares about — the `[ghost]` section
//! plus the `[bridge]` and `[general]` blocks that every sidecar needs.
//!
//! ## First-run secret generation
//!
//! `ghost_secret` and `instance_id` are auto-generated on first run, same
//! way `ultron-core` generates the bridge token. If they're missing from
//! the config we write them back into the on-disk file so the second
//! invocation finds them. This means *every node on the LAN must hand-copy
//! the secret to other nodes before they can see each other* — that's the
//! whole point: privacy by default, explicit opt-in to a shared cluster.

use anyhow::{Context, Result};
use rand::RngCore;
use serde::Deserialize;
use std::path::{Path, PathBuf};

/// What we read out of `[bridge]`. Mirrors the equivalent struct in
/// every other sidecar.
#[derive(Debug, Clone, Deserialize)]
pub struct CoreConfigBridge {
    pub bind: String,
    pub token: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct CoreConfigGeneral {
    pub data_dir: PathBuf,
}

/// `[ghost]` block. All fields have sensible defaults; the file format
/// is forward-compatible.
#[derive(Debug, Clone, Deserialize)]
pub struct GhostConfig {
    #[serde(default = "default_enabled")]
    pub enabled: bool,
    /// Shared secret across all LAN peers in one cluster. Empty on first
    /// run; we generate one and write it back.
    #[serde(default)]
    pub ghost_secret: String,
    /// Stable random suffix used in the mDNS service name. Survives
    /// restarts. Empty on first run; we generate one and write it back.
    #[serde(default)]
    pub instance_id: String,
    /// TCP port the ghost listener binds to. `0` means OS-assigned —
    /// useful for tests; production should pin a port so peers can
    /// reliably reconnect to the same endpoint.
    #[serde(default = "default_port")]
    pub port: u16,
    /// Which event kinds we export to LAN peers.
    #[serde(default = "default_export_kinds")]
    pub export_kinds: Vec<String>,
    /// Log every Nth outbound frame to the Quantum Log. `0` disables.
    #[serde(default = "default_log_every_n_frames")]
    pub log_every_n_frames: u64,
}

fn default_enabled() -> bool {
    true
}
fn default_port() -> u16 {
    9421
}
fn default_export_kinds() -> Vec<String> {
    vec![
        "insight_snapshot".into(),
        "tension_changed".into(),
        "patterns_update".into(),
    ]
}
fn default_log_every_n_frames() -> u64 {
    50
}

impl Default for GhostConfig {
    fn default() -> Self {
        Self {
            enabled: default_enabled(),
            ghost_secret: String::new(),
            instance_id: String::new(),
            port: default_port(),
            export_kinds: default_export_kinds(),
            log_every_n_frames: default_log_every_n_frames(),
        }
    }
}

/// The portion of `config.toml` the ghost sidecar reads.
#[derive(Debug, Clone, Deserialize)]
pub struct GhostMainConfig {
    pub bridge: CoreConfigBridge,
    pub general: CoreConfigGeneral,
    #[serde(default)]
    pub ghost: GhostConfig,
}

pub fn config_path() -> Result<PathBuf> {
    if let Ok(p) = std::env::var("ULTRON_CONFIG") {
        return Ok(PathBuf::from(p));
    }
    let base = dirs::config_dir().context("no config dir on this OS")?;
    Ok(base.join("ULTRON").join("config.toml"))
}

/// Load and parse the config. Does NOT mutate the file — `ensure_secrets`
/// is the only function that touches disk.
pub fn load(path: &Path) -> Result<GhostMainConfig> {
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("read {}", path.display()))?;
    let cfg: GhostMainConfig = toml::from_str(&text).context("parse config.toml")?;
    Ok(cfg)
}

/// Ensure `ghost_secret` and `instance_id` are populated. If either is
/// missing/empty, generate a random value and write the new config back
/// to `path` in-place. Returns the resolved (possibly amended) config.
///
/// We rewrite the file via a generic TOML round-trip: parse → mutate
/// the table → re-serialise. This preserves the rest of the file (other
/// sections, comments may NOT be preserved — TOML round-tripping is
/// lossy on comments, which matches how the core's bootstrap behaves).
pub fn ensure_secrets(path: &Path, cfg: &mut GhostMainConfig) -> Result<()> {
    let mut changed = false;
    if cfg.ghost.ghost_secret.trim().is_empty() {
        cfg.ghost.ghost_secret = random_hex(32); // 64 hex chars = 32 bytes
        changed = true;
    }
    if cfg.ghost.instance_id.trim().is_empty() {
        cfg.ghost.instance_id = random_hex(4); // 8 hex chars
        changed = true;
    }
    if !changed {
        return Ok(());
    }

    // Read the existing TOML as a generic table, splice in the new
    // `[ghost]` section, write it back. Anything we don't know stays
    // verbatim. (Comments are lost — the core's bootstrap has the same
    // limitation; not worth a special parser for the comment edge.)
    let raw = std::fs::read_to_string(path)
        .with_context(|| format!("read {}", path.display()))?;
    let mut doc: toml::Table = toml::from_str(&raw).context("parse existing toml")?;

    let ghost_table = doc
        .entry("ghost".to_string())
        .or_insert_with(|| toml::Value::Table(toml::Table::new()));
    if let toml::Value::Table(t) = ghost_table {
        t.insert(
            "ghost_secret".into(),
            toml::Value::String(cfg.ghost.ghost_secret.clone()),
        );
        t.insert(
            "instance_id".into(),
            toml::Value::String(cfg.ghost.instance_id.clone()),
        );
    } else {
        anyhow::bail!("[ghost] exists but is not a table");
    }

    let serialized = toml::to_string_pretty(&doc).context("re-serialise toml")?;
    std::fs::write(path, serialized)
        .with_context(|| format!("write {}", path.display()))?;
    Ok(())
}

/// Produce a random lowercase-hex string of `n` bytes (so `2*n` chars).
pub fn random_hex(n: usize) -> String {
    let mut buf = vec![0u8; n];
    rand::rngs::OsRng.fill_bytes(&mut buf);
    hex::encode(buf)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn write_tmp(contents: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!(
            "ultron_ghost_cfg_{}_{}.toml",
            std::process::id(),
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        ));
        let mut f = std::fs::File::create(&p).unwrap();
        f.write_all(contents.as_bytes()).unwrap();
        p
    }

    const MIN_CONFIG: &str = r#"
[bridge]
bind = "127.0.0.1:9420"
token = "abc"

[general]
data_dir = "/tmp/ultron"

[ghost]
enabled = true
"#;

    #[test]
    fn load_with_minimal_config_uses_defaults() {
        let p = write_tmp(MIN_CONFIG);
        let cfg = load(&p).unwrap();
        assert_eq!(cfg.ghost.port, 9421);
        assert!(cfg.ghost.export_kinds.contains(&"insight_snapshot".into()));
        assert_eq!(cfg.ghost.log_every_n_frames, 50);
        // Secrets not set yet.
        assert!(cfg.ghost.ghost_secret.is_empty());
        assert!(cfg.ghost.instance_id.is_empty());
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn ensure_secrets_generates_on_first_run() {
        let p = write_tmp(MIN_CONFIG);
        let mut cfg = load(&p).unwrap();
        ensure_secrets(&p, &mut cfg).unwrap();
        assert_eq!(cfg.ghost.ghost_secret.len(), 64, "32 bytes hex = 64 chars");
        assert_eq!(cfg.ghost.instance_id.len(), 8, "4 bytes hex = 8 chars");
        // Second load reads back the values that were written.
        let cfg2 = load(&p).unwrap();
        assert_eq!(cfg2.ghost.ghost_secret, cfg.ghost.ghost_secret);
        assert_eq!(cfg2.ghost.instance_id, cfg.ghost.instance_id);
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn ensure_secrets_preserves_existing_values() {
        let preset = r#"
[bridge]
bind = "127.0.0.1:9420"
token = "abc"

[general]
data_dir = "/tmp/ultron"

[ghost]
enabled = true
ghost_secret = "deadbeefcafebabe00112233aa"
instance_id = "abcd1234"
"#;
        let p = write_tmp(preset);
        let mut cfg = load(&p).unwrap();
        ensure_secrets(&p, &mut cfg).unwrap();
        assert_eq!(cfg.ghost.ghost_secret, "deadbeefcafebabe00112233aa");
        assert_eq!(cfg.ghost.instance_id, "abcd1234");
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn random_hex_is_lowercase_and_correct_length() {
        let a = random_hex(16);
        let b = random_hex(16);
        assert_eq!(a.len(), 32);
        assert_eq!(b.len(), 32);
        assert_ne!(a, b);
        assert!(a.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
    }

    #[test]
    fn default_export_kinds_match_spec() {
        let k = default_export_kinds();
        assert_eq!(k.len(), 3);
        assert!(k.contains(&"insight_snapshot".into()));
        assert!(k.contains(&"tension_changed".into()));
        assert!(k.contains(&"patterns_update".into()));
    }
}
