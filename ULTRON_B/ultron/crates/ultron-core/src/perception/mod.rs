//! # Perception (Phase 1, Module H)
//!
//! Real "eyes and ears" for ULTRON:
//!
//! - [`metrics::InputMetricsAggregator`] — fuses raw input signals into
//!   computed metrics (WPM, backspace storms, mouse hesitation, etc.) on
//!   a periodic tick.
//! - [`window_tracker::WindowTracker`] — polls the foreground window via
//!   Win32 and emits `WindowChanged` events on transitions.
//! - [`screenshot::Screenshotter`] — captures the primary monitor on
//!   demand or on a configurable interval, using BitBlt + GetDIBits.
//!
//! Everything publishes to the existing event bus and (sampled) the Quantum
//! Log; nothing has its own side-channel. New event types are defined in
//! `ultron_types::events` so the WebSocket bridge surfaces them automatically.

pub mod metrics;
pub mod screenshot;
pub mod window_tracker;

pub use metrics::InputMetricsAggregator;
pub use screenshot::Screenshotter;
pub use window_tracker::WindowTracker;
