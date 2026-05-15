//! In-memory registry of currently-known peers.
//!
//! `discovery.rs` populates this via mDNS browse callbacks; `publisher.rs`
//! reads it to know who to fan a frame out to. The map is wrapped in an
//! `Arc<Mutex<...>>` so both tasks can hold it without taking a strong
//! design dependency on async machinery.
//!
//! ## Churn semantics
//!
//! - **Join**: idempotent. Calling `upsert` with the same `peer_id` twice
//!   is fine; the entry's `last_seen_ms` advances each call. This is the
//!   behaviour we want when mDNS re-announces an existing peer.
//! - **Leave**: removing a peer that isn't in the map is a no-op. This
//!   matches mDNS's "peer left" messages which can fire for an entry we
//!   never saw in the first place.
//! - **Address change**: if a peer's IP shifts (laptop unplugged from
//!   Ethernet to WiFi), `upsert` updates `addr` in place — same logical
//!   peer, new transport endpoint.

use parking_lot::Mutex;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;

/// Stable per-peer ID. We use the value derived in `crypto::compute_sender_id`
/// so the map keys match what other peers will send in their `GhostFrame`.
/// String-typed for serde simplicity; opaque otherwise.
pub type PeerId = String;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PeerEntry {
    pub peer_id: PeerId,
    pub addr: SocketAddr,
    /// When this peer was most recently seen via mDNS (Unix epoch ms).
    /// Lets the publisher implement freshness rules later if needed.
    pub last_seen_ms: i64,
}

#[derive(Clone, Default)]
pub struct PeerMap {
    inner: Arc<Mutex<HashMap<PeerId, PeerEntry>>>,
}

impl PeerMap {
    pub fn new() -> Self {
        Self::default()
    }

    /// Add or refresh a peer. Returns `true` if this was a fresh
    /// insertion (caller may want to log a join event); `false` for
    /// a refresh of an existing peer.
    pub fn upsert(&self, peer_id: &str, addr: SocketAddr, now_ms: i64) -> bool {
        let mut g = self.inner.lock();
        let fresh = !g.contains_key(peer_id);
        g.insert(
            peer_id.to_string(),
            PeerEntry {
                peer_id: peer_id.to_string(),
                addr,
                last_seen_ms: now_ms,
            },
        );
        fresh
    }

    /// Remove a peer. Returns the removed entry if it existed.
    pub fn remove(&self, peer_id: &str) -> Option<PeerEntry> {
        self.inner.lock().remove(peer_id)
    }

    pub fn len(&self) -> usize {
        self.inner.lock().len()
    }

    pub fn is_empty(&self) -> bool {
        self.inner.lock().is_empty()
    }

    pub fn contains(&self, peer_id: &str) -> bool {
        self.inner.lock().contains_key(peer_id)
    }

    pub fn get(&self, peer_id: &str) -> Option<PeerEntry> {
        self.inner.lock().get(peer_id).cloned()
    }

    /// Snapshot of every known peer. Returned as `Vec<PeerEntry>` (a
    /// fresh clone) so the caller can iterate without holding the
    /// mutex — important because the publisher's per-peer send may
    /// take a tcp_write which we don't want to do under the lock.
    pub fn snapshot(&self) -> Vec<PeerEntry> {
        self.inner.lock().values().cloned().collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::{IpAddr, Ipv4Addr};

    fn addr(port: u16) -> SocketAddr {
        SocketAddr::new(IpAddr::V4(Ipv4Addr::new(192, 168, 1, 5)), port)
    }

    #[test]
    fn upsert_fresh_returns_true() {
        let m = PeerMap::new();
        assert!(m.upsert("a", addr(9421), 100));
        assert_eq!(m.len(), 1);
    }

    #[test]
    fn upsert_existing_returns_false() {
        let m = PeerMap::new();
        m.upsert("a", addr(9421), 100);
        assert!(!m.upsert("a", addr(9421), 200), "second upsert is refresh");
        assert_eq!(m.len(), 1);
    }

    #[test]
    fn upsert_refresh_updates_last_seen_and_addr() {
        let m = PeerMap::new();
        m.upsert("a", addr(9421), 100);
        m.upsert("a", addr(9422), 250);
        let entry = m.get("a").unwrap();
        assert_eq!(entry.addr, addr(9422));
        assert_eq!(entry.last_seen_ms, 250);
    }

    #[test]
    fn remove_returns_entry() {
        let m = PeerMap::new();
        m.upsert("a", addr(9421), 100);
        let removed = m.remove("a");
        assert!(removed.is_some());
        assert!(m.is_empty());
    }

    #[test]
    fn remove_missing_is_noop() {
        let m = PeerMap::new();
        assert!(m.remove("ghost").is_none());
        assert!(m.is_empty());
    }

    #[test]
    fn churn_idempotency_under_burst() {
        // mDNS sometimes fires the same peer-announce repeatedly; we
        // shouldn't grow the map or double-emit join events.
        let m = PeerMap::new();
        let mut fresh_count = 0;
        for _ in 0..50 {
            if m.upsert("peer-A", addr(9421), 100) {
                fresh_count += 1;
            }
        }
        assert_eq!(fresh_count, 1);
        assert_eq!(m.len(), 1);
    }

    #[test]
    fn snapshot_is_independent_of_map() {
        let m = PeerMap::new();
        m.upsert("a", addr(9421), 1);
        m.upsert("b", addr(9422), 2);
        let snap = m.snapshot();
        assert_eq!(snap.len(), 2);
        // Mutating the map doesn't affect the snapshot.
        m.remove("a");
        assert_eq!(snap.len(), 2);
        assert_eq!(m.len(), 1);
    }

    #[test]
    fn clone_shares_state() {
        // Critical for passing the map between tasks.
        let m1 = PeerMap::new();
        let m2 = m1.clone();
        m1.upsert("a", addr(9421), 100);
        assert_eq!(m2.len(), 1);
    }
}
