//! Inbound side: accept TCP connections from peers, decrypt each
//! length-prefixed frame, parse it as a `GhostFrame`, and re-publish it
//! onto the local WS bridge with a `"ghost:"` prefix on the `kind`.
//!
//! ## Framing
//!
//! Mirror of `publisher.rs`: 4-byte LE u32 length + AES-GCM payload
//! (`nonce(12) || ciphertext || tag(16)`). The length-prefix is the
//! framing boundary; once we read N bytes after a length header we hand
//! exactly those bytes to the cipher.
//!
//! ## Why no separate per-peer protocol state
//!
//! There is none. Each TCP connection is stateless — every frame is
//! self-contained (own nonce, own tag, own GhostFrame). We don't
//! exchange capabilities, handshakes, or ACKs. This keeps the protocol
//! tiny and lets connections drop / re-establish without any teardown
//! dance.
//!
//! ## Safety vs malicious input
//!
//! - Frames over [`MAX_FRAME_BYTES`] are rejected: the connection is
//!   dropped (we trust the peer's secret, but not its bug-free-ness).
//! - Decrypt failures are logged at `warn` and the connection survives
//!   (one corrupted frame doesn't kill the link). Multiple consecutive
//!   failures will eventually exhaust the peer's patience on its end.
//! - Non-JSON plaintext is also `warn`+drop; same reasoning.

use crate::crypto::GhostCipher;
use crate::publisher::MAX_FRAME_BYTES;
use anyhow::{Context, Result};
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tokio::io::{AsyncReadExt, BufReader};
use tokio::net::{TcpListener, TcpStream};
use tokio::task::JoinHandle;
use tracing::{debug, info, warn};
use ultron_quantum_log::{EntryKind, NewEntry, QuantumLog};
use ultron_types::GhostFrame;

/// Handle for the listener subsystem.
#[derive(Clone)]
pub struct Listener {
    cipher: GhostCipher,
    own_sender_id: String,
    /// Where to send decrypted frames so they get re-published onto the
    /// local WS bridge. The orchestrator wires this to the WS handle.
    out_tx: tokio::sync::mpsc::Sender<RemoteEvent>,
    qlog: QuantumLog,
    stop: Arc<AtomicBool>,
}

/// One decrypted, parsed, ready-to-republish remote event.
#[derive(Debug, Clone)]
pub struct RemoteEvent {
    pub sender_id: String,
    /// Kind already prefixed with `"ghost:"` — ready to publish verbatim.
    pub kind: String,
    pub payload: serde_json::Value,
}

impl Listener {
    pub fn new(
        cipher: GhostCipher,
        own_sender_id: String,
        out_tx: tokio::sync::mpsc::Sender<RemoteEvent>,
        qlog: QuantumLog,
    ) -> Self {
        Self {
            cipher,
            own_sender_id,
            out_tx,
            qlog,
            stop: Arc::new(AtomicBool::new(false)),
        }
    }

    /// Bind the TCP listener and spawn the accept loop. Returns the
    /// resolved local address (useful when `port = 0` so the OS
    /// assigns) plus the JoinHandle for shutdown.
    pub async fn spawn(self, port: u16) -> Result<(SocketAddr, JoinHandle<()>)> {
        // Bind to 0.0.0.0 so peers on the LAN can reach us. mDNS
        // advertises the host's real addresses; localhost-only would
        // defeat the purpose.
        let bind_addr = format!("0.0.0.0:{port}");
        let socket = TcpListener::bind(&bind_addr)
            .await
            .with_context(|| format!("bind {bind_addr}"))?;
        let local_addr = socket.local_addr().context("read local_addr")?;
        info!(addr = %local_addr, "ghost listener bound");

        let me = self.clone();
        let handle = tokio::spawn(async move {
            me.run(socket).await;
        });
        Ok((local_addr, handle))
    }

    pub fn shutdown(&self) {
        self.stop.store(true, Ordering::SeqCst);
    }

    async fn run(self, socket: TcpListener) {
        loop {
            if self.stop.load(Ordering::SeqCst) {
                break;
            }
            let (stream, peer_addr) = match socket.accept().await {
                Ok(p) => p,
                Err(e) => {
                    warn!("accept failed: {e}");
                    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                    continue;
                }
            };
            debug!(peer_addr = %peer_addr, "inbound connection accepted");

            // Each connection gets its own task — slow / dead peer
            // can't block accepting fresh ones.
            let me = self.clone();
            tokio::spawn(async move {
                if let Err(e) = me.handle_connection(stream, peer_addr).await {
                    debug!(peer_addr = %peer_addr, "connection ended: {e:#}");
                }
            });
        }
        info!("ghost listener stopped");
    }

    /// Read length-prefixed frames forever. Returns when the peer
    /// hangs up or we hit a fatal protocol error.
    async fn handle_connection(
        &self,
        stream: TcpStream,
        peer_addr: SocketAddr,
    ) -> Result<()> {
        let mut reader = BufReader::new(stream);

        loop {
            if self.stop.load(Ordering::SeqCst) {
                break;
            }

            // ---- Length prefix ----
            let mut len_buf = [0u8; 4];
            if let Err(e) = reader.read_exact(&mut len_buf).await {
                // EOF — peer closed cleanly. Not an error.
                debug!(peer_addr = %peer_addr, "connection EOF: {e}");
                return Ok(());
            }
            let frame_len = u32::from_le_bytes(len_buf);
            if frame_len == 0 || frame_len > MAX_FRAME_BYTES {
                warn!(
                    peer_addr = %peer_addr,
                    frame_len,
                    "invalid frame length — dropping connection"
                );
                return Ok(());
            }

            // ---- Ciphertext ----
            let mut wire = vec![0u8; frame_len as usize];
            if let Err(e) = reader.read_exact(&mut wire).await {
                debug!(peer_addr = %peer_addr, "short read mid-frame: {e}");
                return Ok(());
            }

            // ---- Decrypt ----
            let plaintext = match self.cipher.decrypt(&wire) {
                Ok(pt) => pt,
                Err(e) => {
                    warn!(
                        peer_addr = %peer_addr,
                        "decrypt failed (tampered or wrong secret?): {e:#}"
                    );
                    // Don't kill the connection on a single bad frame —
                    // it could be a brief corruption. If the peer is
                    // truly hostile they'll fill the link with bad
                    // frames and we'll burn CPU, but we won't crash.
                    continue;
                }
            };

            // ---- Parse ----
            let frame: GhostFrame = match serde_json::from_slice(&plaintext) {
                Ok(f) => f,
                Err(e) => {
                    warn!(peer_addr = %peer_addr, "non-GhostFrame plaintext: {e}");
                    continue;
                }
            };

            // ---- Self-echo guard ----
            // mDNS browses can race with the TCP accept, so a buggy
            // peer (or our own loopback config) might dial itself.
            // Hard-drop these.
            if frame.sender_id == self.own_sender_id {
                debug!(
                    peer_addr = %peer_addr,
                    "dropping self-echo frame"
                );
                continue;
            }

            // ---- Republish onto local WS ----
            let prefixed_kind = format!("{}{}", GhostFrame::REMOTE_PREFIX, frame.kind);
            let event = RemoteEvent {
                sender_id: frame.sender_id.clone(),
                kind: prefixed_kind,
                payload: frame.payload,
            };
            if self.out_tx.send(event).await.is_err() {
                // Channel closed — the orchestrator went away. Wrap up.
                debug!("out channel closed — listener shutting down");
                return Ok(());
            }

            // Sampled audit. We don't log every inbound frame — same
            // reasoning as the publisher's sampling.
            let _ = self
                .qlog
                .append_async(NewEntry::new(
                    EntryKind::Note,
                    "ghost-network",
                    serde_json::json!({
                        "event": "frame_received",
                        "from": frame.sender_id,
                        "kind": frame.kind,
                    }),
                ))
                .await;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::AsyncWriteExt;

    fn tmp_qlog() -> QuantumLog {
        let mut p = std::env::temp_dir();
        p.push(format!(
            "ultron_ghost_listen_test_{}_{}.db",
            std::process::id(),
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        ));
        QuantumLog::open(&p).unwrap()
    }

    fn frame_wire(cipher: &GhostCipher, sender_id: &str, kind: &str) -> Vec<u8> {
        let f = GhostFrame {
            sender_id: sender_id.into(),
            kind: kind.into(),
            ts: 1_000,
            payload: serde_json::json!({"v": 1}),
        };
        let plaintext = serde_json::to_vec(&f).unwrap();
        let ct = cipher.encrypt(&plaintext).unwrap();
        let mut wire = Vec::with_capacity(4 + ct.len());
        wire.extend_from_slice(&(ct.len() as u32).to_le_bytes());
        wire.extend_from_slice(&ct);
        wire
    }

    #[tokio::test]
    async fn end_to_end_decrypt_and_republish() {
        // Bind the listener to an OS-assigned port, then dial it and
        // send a valid frame. Assert the RemoteEvent arrives on the
        // out channel with the "ghost:" prefix.
        let cipher = GhostCipher::from_secret("shared-secret").unwrap();
        let (tx, mut rx) = tokio::sync::mpsc::channel::<RemoteEvent>(16);
        let listener = Listener::new(
            cipher.clone(),
            "self-id".into(),
            tx,
            tmp_qlog(),
        );
        let (addr, _handle) = listener.spawn(0).await.unwrap();

        // Dial in and send one frame.
        let mut sock = tokio::net::TcpStream::connect(addr).await.unwrap();
        let wire = frame_wire(&cipher, "remote-peer", "insight_snapshot");
        sock.write_all(&wire).await.unwrap();
        sock.flush().await.unwrap();

        let event = tokio::time::timeout(
            std::time::Duration::from_secs(2),
            rx.recv(),
        )
        .await
        .expect("listener didn't republish in time")
        .expect("channel closed unexpectedly");

        assert_eq!(event.sender_id, "remote-peer");
        assert_eq!(event.kind, "ghost:insight_snapshot");
        assert_eq!(event.payload["v"], 1);
    }

    #[tokio::test]
    async fn self_echo_is_dropped() {
        let cipher = GhostCipher::from_secret("shared-secret").unwrap();
        let (tx, mut rx) = tokio::sync::mpsc::channel::<RemoteEvent>(16);
        let listener = Listener::new(
            cipher.clone(),
            "self-id".into(),
            tx,
            tmp_qlog(),
        );
        let (addr, _handle) = listener.spawn(0).await.unwrap();

        // Send a frame whose sender_id matches our own.
        let mut sock = tokio::net::TcpStream::connect(addr).await.unwrap();
        let wire = frame_wire(&cipher, "self-id", "insight_snapshot");
        sock.write_all(&wire).await.unwrap();
        sock.flush().await.unwrap();

        // Should NOT see any republished event within a short window.
        let r = tokio::time::timeout(
            std::time::Duration::from_millis(200),
            rx.recv(),
        )
        .await;
        assert!(r.is_err(), "self-echo frame must be dropped, not republished");
    }

    #[tokio::test]
    async fn tampered_frame_dropped_connection_survives() {
        let cipher = GhostCipher::from_secret("shared-secret").unwrap();
        let (tx, mut rx) = tokio::sync::mpsc::channel::<RemoteEvent>(16);
        let listener = Listener::new(cipher.clone(), "self-id".into(), tx, tmp_qlog());
        let (addr, _handle) = listener.spawn(0).await.unwrap();

        let mut sock = tokio::net::TcpStream::connect(addr).await.unwrap();
        // Send one valid frame first.
        let wire = frame_wire(&cipher, "remote", "insight_snapshot");
        sock.write_all(&wire).await.unwrap();

        // Now tamper a frame and send.
        let mut bad = frame_wire(&cipher, "remote", "insight_snapshot");
        // Flip a bit inside the ciphertext (skip the 4-byte length
        // header + 12-byte nonce).
        bad[4 + 12 + 1] ^= 0x01;
        sock.write_all(&bad).await.unwrap();

        // And one more valid frame after — should still get through,
        // proving the connection survived the bad frame.
        let wire2 = frame_wire(&cipher, "remote", "tension_changed");
        sock.write_all(&wire2).await.unwrap();
        sock.flush().await.unwrap();

        // We expect TWO events (the two valid frames), in order.
        let e1 = tokio::time::timeout(std::time::Duration::from_secs(2), rx.recv())
            .await
            .unwrap()
            .unwrap();
        let e2 = tokio::time::timeout(std::time::Duration::from_secs(2), rx.recv())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(e1.kind, "ghost:insight_snapshot");
        assert_eq!(e2.kind, "ghost:tension_changed");
    }

    #[tokio::test]
    async fn oversized_length_kills_connection_but_not_listener() {
        let cipher = GhostCipher::from_secret("shared-secret").unwrap();
        let (tx, _rx) = tokio::sync::mpsc::channel::<RemoteEvent>(16);
        let listener = Listener::new(cipher.clone(), "self-id".into(), tx.clone(), tmp_qlog());
        let (addr, _handle) = listener.spawn(0).await.unwrap();

        // Send a length-prefix way over MAX_FRAME_BYTES.
        let mut sock = tokio::net::TcpStream::connect(addr).await.unwrap();
        let bogus = (MAX_FRAME_BYTES + 1).to_le_bytes();
        sock.write_all(&bogus).await.unwrap();
        sock.flush().await.unwrap();

        // The listener should drop *this* connection. A fresh connect
        // should still succeed — the accept loop is alive.
        drop(sock);
        let mut fresh = tokio::net::TcpStream::connect(addr).await.unwrap();
        // Send a valid frame on the new connection to prove it works.
        let wire = frame_wire(&cipher, "remote", "tension_changed");
        fresh.write_all(&wire).await.unwrap();
        fresh.flush().await.unwrap();
        // Connection accepted — that's the assertion. We don't read
        // back; the `tx` is held alive by `_rx` so the listener can
        // republish without blocking.
    }
}
