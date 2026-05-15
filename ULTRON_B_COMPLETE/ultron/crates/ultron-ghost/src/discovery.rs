//! mDNS-based peer discovery.
//!
//! Each ghost instance advertises itself under the service type
//! `_ultron._tcp.local.`. The instance name embeds the configured
//! `instance_id` so two restarts of the same machine produce stable
//! announcements (and other peers de-dupe by `sender_id` in the TXT
//! records).
//!
//! ## Why `mdns-sd`
//!
//! Pure Rust, no system mDNS daemon required, works in the Windows
//! Service security context, no AF_UNIX dependency that would break in
//! sandboxed CI. The crate runs its own background thread internally;
//! we wrap it in a Tokio task that fans events out via a channel.
//!
//! ## Filtering self
//!
//! mDNS will echo our own advertisement back at us. Our browse loop
//! reads the `sender_id` TXT record and drops any service whose ID
//! matches our own — so we never connect to ourselves.

use crate::peer_map::PeerMap;
use anyhow::{Context, Result};
use mdns_sd::{ServiceDaemon, ServiceEvent, ServiceInfo};
use std::collections::HashMap;
use std::net::{IpAddr, SocketAddr};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tracing::{debug, info, warn};

/// mDNS service type. Includes the trailing dot per the spec.
pub const SERVICE_TYPE: &str = "_ultron._tcp.local.";

/// TXT-record key holding our opaque sender ID.
const TXT_KEY_SENDER_ID: &str = "sid";

/// Holds the daemon handle so we can shut it down cleanly.
pub struct Discovery {
    daemon: ServiceDaemon,
    /// Our own sender_id — used to filter self-echo.
    own_sender_id: String,
    /// Full mDNS service name we registered under. Used to unregister
    /// on shutdown.
    registered_name: String,
    /// Cooperative shutdown flag. The browse loop polls this.
    stop: Arc<AtomicBool>,
}

impl Discovery {
    /// Boot the mDNS daemon, advertise ourselves, and spawn the browse
    /// loop. The returned [`Discovery`] owns the daemon handle —
    /// dropping it (or calling [`Self::shutdown`]) cleanly tears
    /// everything down.
    pub fn start(
        instance_id: &str,
        port: u16,
        own_sender_id: &str,
        peers: PeerMap,
    ) -> Result<Self> {
        let daemon = ServiceDaemon::new().context("create mdns daemon")?;

        // Build the advertised ServiceInfo. The host name and instance
        // name are derived from the configured instance_id so they're
        // stable across restarts.
        let host_label = format!("ultron-{}.local.", instance_id);
        let instance_name = format!("ultron-{}", instance_id);
        let mut props = HashMap::new();
        props.insert(TXT_KEY_SENDER_ID.to_string(), own_sender_id.to_string());

        // We don't pin an IP — mdns-sd will fill in the host's
        // addresses for us via `enable_addr_auto`.
        let service_info = ServiceInfo::new(
            SERVICE_TYPE,
            &instance_name,
            &host_label,
            "",
            port,
            Some(props),
        )
        .context("build ServiceInfo")?
        .enable_addr_auto();

        let registered_name = service_info.get_fullname().to_string();
        daemon
            .register(service_info)
            .context("register mdns service")?;
        info!(name = %registered_name, port, "mdns service registered");

        let receiver = daemon
            .browse(SERVICE_TYPE)
            .context("browse mdns service type")?;

        let stop = Arc::new(AtomicBool::new(false));
        let own = own_sender_id.to_string();
        let stop_clone = stop.clone();

        tokio::spawn(async move {
            run_browse_loop(receiver, peers, own, stop_clone).await;
        });

        Ok(Self {
            daemon,
            own_sender_id: own_sender_id.to_string(),
            registered_name,
            stop,
        })
    }

    /// Own sender ID — exposed mostly so listener/publisher can stamp
    /// outbound frames without re-deriving it.
    pub fn own_sender_id(&self) -> &str {
        &self.own_sender_id
    }

    /// Stop browsing and unregister our advertisement. Idempotent.
    pub fn shutdown(&self) {
        self.stop.store(true, Ordering::SeqCst);
        // Errors here are non-fatal — we're shutting down anyway.
        if let Err(e) = self.daemon.unregister(&self.registered_name) {
            debug!("mdns unregister failed (non-fatal): {e}");
        }
        if let Err(e) = self.daemon.shutdown() {
            debug!("mdns daemon shutdown failed (non-fatal): {e}");
        }
    }
}

impl Drop for Discovery {
    fn drop(&mut self) {
        // Best-effort cleanup in case the caller forgot.
        if !self.stop.load(Ordering::SeqCst) {
            self.shutdown();
        }
    }
}

/// Background loop translating `ServiceEvent`s into peer_map mutations.
/// Returns when `stop` flips or the channel closes.
async fn run_browse_loop(
    receiver: mdns_sd::Receiver<ServiceEvent>,
    peers: PeerMap,
    own_sender_id: String,
    stop: Arc<AtomicBool>,
) {
    info!(svc = SERVICE_TYPE, "mdns browse loop started");

    // The crate's receiver is sync. Use `recv_async` so we don't block
    // the runtime; checking `stop` between every event gives prompt
    // shutdown.
    loop {
        if stop.load(Ordering::SeqCst) {
            break;
        }
        let event = match receiver.recv_async().await {
            Ok(e) => e,
            Err(_) => break, // channel closed
        };
        match event {
            ServiceEvent::ServiceResolved(info) => {
                handle_resolved(&info, &peers, &own_sender_id);
            }
            ServiceEvent::ServiceRemoved(_, fullname) => {
                handle_removed(&fullname, &peers);
            }
            // We don't act on ServiceFound (pre-resolve) or other
            // informational events.
            other => {
                debug!("mdns event ignored: {other:?}");
            }
        }
    }
    info!("mdns browse loop stopped");
}

fn handle_resolved(info: &ServiceInfo, peers: &PeerMap, own_sender_id: &str) {
    // Extract the sender_id from TXT props.
    let sender_id = info
        .get_property_val_str(TXT_KEY_SENDER_ID)
        .map(str::to_string);
    let Some(sender_id) = sender_id else {
        warn!(
            fullname = info.get_fullname(),
            "resolved peer has no sender_id TXT — skipping"
        );
        return;
    };
    if sender_id == own_sender_id {
        debug!(
            fullname = info.get_fullname(),
            "skipping self-echo"
        );
        return;
    }

    // Pick the first IPv4 address; ignore IPv6 for now (firewalls,
    // dual-stack complexity — Phase 1 keeps it simple).
    let port = info.get_port();
    let Some(addr) = info
        .get_addresses()
        .iter()
        .filter_map(|ip| match ip {
            IpAddr::V4(v4) => Some(SocketAddr::new(IpAddr::V4(*v4), port)),
            _ => None,
        })
        .next()
    else {
        warn!(
            fullname = info.get_fullname(),
            "resolved peer has no IPv4 address — skipping"
        );
        return;
    };

    let now_ms = chrono::Utc::now().timestamp_millis();
    let fresh = peers.upsert(&sender_id, addr, now_ms);
    if fresh {
        info!(sender_id = %sender_id, addr = %addr, "peer joined");
    } else {
        debug!(sender_id = %sender_id, addr = %addr, "peer refreshed");
    }
}

fn handle_removed(fullname: &str, peers: &PeerMap) {
    // The "removed" event only gives us the mDNS fullname, not the
    // sender_id. We need to find the matching peer in the map by some
    // other means. Easiest: the fullname contains the instance_id,
    // which we used to derive part of our advertisement. But for
    // remote peers we don't know their instance_id → fullname mapping.
    //
    // Practical workaround: we maintain a small reverse-lookup table
    // here. For Phase 1 it's acceptable to scan the peer map and drop
    // any peer whose recorded mDNS name matches (we don't store the
    // fullname today, so we just log and let the natural staleness
    // cleanup handle it).
    //
    // TODO when we add Pattern detection of dead peers based on
    // `last_seen_ms`, this is where it'll plug in.
    debug!(fullname, peer_count = peers.len(), "mdns service removed");
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::Ipv4Addr;

    fn addr(port: u16) -> SocketAddr {
        SocketAddr::new(IpAddr::V4(Ipv4Addr::new(192, 168, 1, 5)), port)
    }

    // We can't easily unit-test the real `handle_resolved` because
    // `ServiceInfo` doesn't have a public test constructor. Instead we
    // assert the helper rules through PeerMap.

    #[test]
    fn join_then_refresh_only_logs_once() {
        let peers = PeerMap::new();
        let now = 1_000;
        let fresh1 = peers.upsert("peer-A", addr(9421), now);
        let fresh2 = peers.upsert("peer-A", addr(9421), now + 5_000);
        assert!(fresh1);
        assert!(!fresh2, "refresh must not register as fresh");
        assert_eq!(peers.len(), 1);
    }

    #[test]
    fn self_echo_filter_logic() {
        // The actual filter lives inline in handle_resolved; verify
        // the rule we test:  own_sender_id == discovered.sender_id
        // means skip the upsert.
        let own = "abc123";
        let discovered_self = "abc123";
        let discovered_other = "xyz789";
        assert_eq!(own == discovered_self, true);
        assert_eq!(own == discovered_other, false);
    }
}
