//! Outbound side: pull local events off the WS bridge, scrub +
//! encrypt them, and fan them out over TCP to every known peer.
//!
//! ## Per-peer connection lifecycle
//!
//! Each discovered peer gets one long-lived TCP connection managed by a
//! dedicated task. The task owns the stream and pulls outbound bytes
//! from a small `tokio::sync::mpsc` channel. On disconnect:
//!
//! 1. The task drops out of its write loop with an error.
//! 2. It sleeps for the next backoff interval (1 → 2 → 4 → 8 → 16 → 30 s,
//!    capped, with ±20% jitter so reconnect storms don't synchronise).
//! 3. It redials the peer's last-known address.
//! 4. On success, backoff resets to 1 s.
//!
//! Peers that leave the mDNS map are torn down by the supervisor loop
//! below: it diffs the peer_map against its known connection set every
//! few seconds and abort()s the tasks for vanished peers.
//!
//! ## Frame ordering
//!
//! Within one peer connection, frames go out in the order they're
//! received from the channel. Across peers there is no global
//! ordering — each peer sees its own copy in arrival order.

use crate::config::GhostConfig;
use crate::crypto::GhostCipher;
use crate::peer_map::PeerMap;
use crate::scrubber::scrub_in_place;
use anyhow::{Context, Result};
use parking_lot::Mutex;
use rand::Rng;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::io::AsyncWriteExt;
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tracing::{debug, info, warn};
use ultron_quantum_log::{EntryKind, NewEntry, QuantumLog};
use ultron_types::GhostFrame;

/// Bounded queue per peer — backpressure rather than unlimited buffering.
/// If a peer is slow, we drop frames rather than blow memory.
const PER_PEER_QUEUE_DEPTH: usize = 64;

/// Reconnect backoff schedule, in seconds. Final value is the cap.
const BACKOFF_SECS: &[u64] = &[1, 2, 4, 8, 16, 30];

/// How often the supervisor loop checks for peer churn.
const SUPERVISOR_TICK: Duration = Duration::from_secs(2);

/// Maximum wire frame size we'll accept on the listener side too —
/// matches the publisher's safety check. 1 MiB is generous for our
/// payloads (the largest realistic snapshot is ~2 KB).
pub const MAX_FRAME_BYTES: u32 = 1024 * 1024;

/// Handle for the publisher subsystem.
///
/// Cloning is cheap (Arc-wrapped state). Drop the original after
/// spawning the supervisor task — the per-peer connector tasks hold
/// their own clones and stay alive until the supervisor aborts them.
#[derive(Clone)]
pub struct Publisher {
    cipher: GhostCipher,
    sender_id: String,
    export_kinds: Arc<Vec<String>>,
    peers: PeerMap,
    qlog: QuantumLog,
    log_every_n_frames: u64,
    /// Live per-peer outbound queues.
    connections: Arc<Mutex<HashMap<String, PeerConnection>>>,
    /// Monotonic counter of outbound frames for the sampled Quantum
    /// Log entry.
    frame_count: Arc<AtomicU64>,
}

struct PeerConnection {
    tx: mpsc::Sender<Vec<u8>>,
    handle: JoinHandle<()>,
}

impl Publisher {
    pub fn new(
        cipher: GhostCipher,
        sender_id: String,
        cfg: &GhostConfig,
        peers: PeerMap,
        qlog: QuantumLog,
    ) -> Self {
        Self {
            cipher,
            sender_id,
            export_kinds: Arc::new(cfg.export_kinds.clone()),
            peers,
            qlog,
            log_every_n_frames: cfg.log_every_n_frames,
            connections: Arc::new(Mutex::new(HashMap::new())),
            frame_count: Arc::new(AtomicU64::new(0)),
        }
    }

    /// `true` if `kind` is in the configured export allowlist.
    pub fn exports(&self, kind: &str) -> bool {
        self.export_kinds.iter().any(|k| k == kind)
    }

    /// Build, scrub, encrypt one frame from a local event and fan it
    /// out to every currently-connected peer.
    pub async fn publish_event(&self, kind: &str, mut payload: serde_json::Value) -> Result<()> {
        if !self.exports(kind) {
            return Ok(());
        }
        // Scrub sensitive fields before anything else touches the
        // payload — defence in depth against accidental logging.
        scrub_in_place(&mut payload);

        let frame = GhostFrame {
            sender_id: self.sender_id.clone(),
            kind: kind.to_string(),
            ts: chrono::Utc::now().timestamp_millis(),
            payload,
        };
        let plaintext = serde_json::to_vec(&frame).context("serialise GhostFrame")?;
        let wire_payload = self.cipher.encrypt(&plaintext)?;

        // Length prefix (4-byte LE u32) + ciphertext.
        if wire_payload.len() > MAX_FRAME_BYTES as usize {
            anyhow::bail!(
                "outbound frame too large: {} > {}",
                wire_payload.len(),
                MAX_FRAME_BYTES
            );
        }
        let mut wire = Vec::with_capacity(4 + wire_payload.len());
        wire.extend_from_slice(&(wire_payload.len() as u32).to_le_bytes());
        wire.extend_from_slice(&wire_payload);

        // Snapshot the connection map, send to each peer non-blockingly
        // (try_send; drop on overflow rather than awaiting). Snapshot
        // avoids holding the mutex across awaits.
        let txs: Vec<(String, mpsc::Sender<Vec<u8>>)> = {
            let g = self.connections.lock();
            g.iter().map(|(id, conn)| (id.clone(), conn.tx.clone())).collect()
        };
        for (peer_id, tx) in txs {
            if let Err(e) = tx.try_send(wire.clone()) {
                match e {
                    mpsc::error::TrySendError::Full(_) => {
                        warn!(peer = %peer_id, "peer queue full — dropping frame");
                    }
                    mpsc::error::TrySendError::Closed(_) => {
                        debug!(peer = %peer_id, "peer channel closed — connection torn down");
                    }
                }
            }
        }

        // Sampled audit log.
        let n = self.frame_count.fetch_add(1, Ordering::Relaxed) + 1;
        if self.log_every_n_frames > 0 && n % self.log_every_n_frames == 0 {
            let _ = self
                .qlog
                .append_async(NewEntry::new(
                    EntryKind::Note,
                    "ghost-network",
                    serde_json::json!({
                        "event": "frame_sent_sampled",
                        "kind": kind,
                        "frame_count": n,
                        "wire_bytes": wire.len(),
                    }),
                ))
                .await;
        }

        Ok(())
    }

    /// Background supervisor: keeps the connection map in sync with the
    /// peer map. Adds tasks for new peers, aborts tasks for departed
    /// peers. Returns its own JoinHandle so main can abort cleanly.
    pub fn spawn_supervisor(self) -> JoinHandle<()> {
        tokio::spawn(async move {
            info!("publisher supervisor started");
            let mut interval = tokio::time::interval(SUPERVISOR_TICK);
            interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            loop {
                interval.tick().await;
                self.reconcile_connections();
            }
            // Unreachable on cancellation; abort()ing the task drops it
            // and the inner per-peer tasks die via the same mechanism.
        })
    }

    fn reconcile_connections(&self) {
        let known_peers = self.peers.snapshot();
        let mut g = self.connections.lock();

        // 1. Remove tasks for peers that are no longer in the map.
        let known_ids: std::collections::HashSet<String> =
            known_peers.iter().map(|p| p.peer_id.clone()).collect();
        let to_drop: Vec<String> = g
            .keys()
            .filter(|id| !known_ids.contains(*id))
            .cloned()
            .collect();
        for id in to_drop {
            if let Some(conn) = g.remove(&id) {
                conn.handle.abort();
                info!(peer = %id, "tearing down dead peer connection");
            }
        }

        // 2. Add tasks for peers we haven't connected to yet.
        for peer in known_peers {
            if g.contains_key(&peer.peer_id) {
                continue;
            }
            let (tx, rx) = mpsc::channel::<Vec<u8>>(PER_PEER_QUEUE_DEPTH);
            let peer_id = peer.peer_id.clone();
            let addr = peer.addr;
            let handle = tokio::spawn(async move {
                run_peer_connection(peer_id.clone(), addr, rx).await;
            });
            g.insert(
                peer.peer_id.clone(),
                PeerConnection { tx, handle },
            );
            info!(peer = %peer.peer_id, addr = %peer.addr, "new peer connection task spawned");
        }
    }
}

/// Per-peer outbound task. Owns one `TcpStream`, pulls frames from
/// `rx`, writes them out. On disconnect, sleeps with exponential
/// backoff + jitter and redials.
async fn run_peer_connection(
    peer_id: String,
    addr: std::net::SocketAddr,
    mut rx: mpsc::Receiver<Vec<u8>>,
) {
    let mut attempt: usize = 0;
    loop {
        // ----- Dial with backoff -----
        if attempt > 0 {
            let base = BACKOFF_SECS[attempt.min(BACKOFF_SECS.len() - 1)] as f64;
            // ±20% jitter to break up reconnect storms.
            let jitter: f64 = rand::thread_rng().gen_range(0.8..1.2);
            let delay = Duration::from_secs_f64(base * jitter);
            debug!(
                peer = %peer_id,
                attempt,
                delay_secs = delay.as_secs_f64(),
                "backoff before redial"
            );
            tokio::time::sleep(delay).await;
        }
        attempt = attempt.saturating_add(1);

        // ----- Connect -----
        let mut stream = match TcpStream::connect(addr).await {
            Ok(s) => s,
            Err(e) => {
                debug!(peer = %peer_id, addr = %addr, "connect failed: {e}");
                continue;
            }
        };
        info!(peer = %peer_id, addr = %addr, "peer connection established");
        attempt = 0; // successful connect resets backoff

        // ----- Write loop -----
        loop {
            let frame = match rx.recv().await {
                Some(f) => f,
                None => {
                    // Channel closed — supervisor tore us down.
                    debug!(peer = %peer_id, "channel closed; task exiting");
                    return;
                }
            };
            if let Err(e) = stream.write_all(&frame).await {
                warn!(peer = %peer_id, "write failed, will reconnect: {e}");
                break;
            }
        }
        // Fall through to redial. rx is preserved across iterations,
        // so frames queued during the disconnect window are delivered
        // once we reconnect (up to PER_PEER_QUEUE_DEPTH; older frames
        // dropped by `try_send`).
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::{IpAddr, Ipv4Addr, SocketAddr};

    fn tmp_qlog() -> QuantumLog {
        let mut p = std::env::temp_dir();
        p.push(format!(
            "ultron_ghost_pub_test_{}_{}.db",
            std::process::id(),
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        ));
        QuantumLog::open(&p).unwrap()
    }

    fn pub_with(export_kinds: Vec<String>) -> Publisher {
        let cipher = GhostCipher::from_secret("test-secret").unwrap();
        let cfg = GhostConfig {
            export_kinds,
            log_every_n_frames: 50,
            ..GhostConfig::default()
        };
        Publisher::new(
            cipher,
            "self-sender-id".into(),
            &cfg,
            PeerMap::new(),
            tmp_qlog(),
        )
    }

    #[test]
    fn exports_respects_allowlist() {
        let p = pub_with(vec!["insight_snapshot".into(), "tension_changed".into()]);
        assert!(p.exports("insight_snapshot"));
        assert!(p.exports("tension_changed"));
        assert!(!p.exports("input_activity"));
        assert!(!p.exports("heartbeat"));
    }

    #[tokio::test]
    async fn publish_event_skips_non_exported_kind() {
        let p = pub_with(vec!["insight_snapshot".into()]);
        // Should be a clean no-op for a non-exported kind; counter not
        // incremented.
        p.publish_event("input_activity", serde_json::json!({"x": 1}))
            .await
            .unwrap();
        assert_eq!(p.frame_count.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn publish_event_scrubs_sensitive_fields() {
        // No peers connected → frame is built + encrypted + sent to
        // zero peers, but we can verify the scrub happened by sniffing
        // the wire format.
        //
        // We can't easily intercept the in-flight ciphertext without
        // wiring up a fake peer; the *real* coverage of scrubbing
        // lives in scrubber.rs. Here we just verify the publish path
        // doesn't panic on payloads with sensitive fields.
        let p = pub_with(vec!["window_changed".into()]);
        p.publish_event(
            "window_changed",
            serde_json::json!({
                "title": "secret window",
                "process_name": "Code.exe",
                "pid": 1234,
            }),
        )
        .await
        .unwrap();
        assert_eq!(p.frame_count.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn reconcile_adds_new_peers_and_drops_old_ones() {
        let p = pub_with(vec!["insight_snapshot".into()]);
        // No peers yet.
        p.reconcile_connections();
        assert_eq!(p.connections.lock().len(), 0);

        // Add a peer to the map.
        p.peers.upsert(
            "peer-A",
            SocketAddr::new(IpAddr::V4(Ipv4Addr::new(127, 0, 0, 1)), 9999),
            1_000,
        );
        p.reconcile_connections();
        assert_eq!(p.connections.lock().len(), 1);
        assert!(p.connections.lock().contains_key("peer-A"));

        // Remove the peer.
        p.peers.remove("peer-A");
        p.reconcile_connections();
        assert_eq!(p.connections.lock().len(), 0);
    }
}
