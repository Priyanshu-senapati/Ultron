//! Typed broadcast bus over `tokio::sync::broadcast`.
//!
//! A slow consumer falls behind and drops messages rather than back-pressuring
//! the producer (correct trade-off for this workload — input events are
//! non-essential historically; the Quantum Log is the durable record).

use tokio::sync::broadcast;
use tracing::warn;
use ultron_types::UltronEvent;

#[derive(Clone)]
pub struct EventBus {
    tx: broadcast::Sender<UltronEvent>,
}

impl EventBus {
    pub fn new(capacity: usize) -> Self {
        let (tx, _) = broadcast::channel(capacity);
        Self { tx }
    }

    pub fn subscribe(&self) -> broadcast::Receiver<UltronEvent> {
        self.tx.subscribe()
    }

    pub fn subscriber_count(&self) -> usize {
        self.tx.receiver_count()
    }

    pub fn publish(&self, ev: UltronEvent) {
        // Err = no subscribers; that's fine, just means nothing's listening.
        let _ = self.tx.send(ev);
    }

    /// Publish but log when no subscribers — useful for sanity-checking
    /// during phase wiring.
    pub fn publish_loud(&self, ev: UltronEvent) {
        if self.tx.send(ev).is_err() {
            warn!("publish dropped: no subscribers on the bus");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn fan_out() {
        let bus = EventBus::new(64);
        let mut a = bus.subscribe();
        let mut b = bus.subscribe();
        bus.publish(UltronEvent::Heartbeat {
            tension: 0.1,
            uptime_secs: 1,
        });
        let ea = a.recv().await.unwrap();
        let eb = b.recv().await.unwrap();
        match (ea, eb) {
            (UltronEvent::Heartbeat { .. }, UltronEvent::Heartbeat { .. }) => (),
            _ => panic!("wrong variant"),
        }
    }
}
