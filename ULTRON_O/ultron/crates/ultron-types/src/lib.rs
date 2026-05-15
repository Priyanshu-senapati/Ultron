//! Shared type vocabulary for ULTRON modules.
//!
//! This crate has zero IO and no platform deps. Anything that crosses
//! a module boundary (event bus, WebSocket, Quantum Log payload) lives here.

pub mod events;
pub mod ghost;
pub mod insight;
pub mod messages;
pub mod perception;
pub mod tension;

pub use events::*;
pub use ghost::*;
pub use insight::*;
pub use messages::*;
pub use perception::*;
pub use tension::*;

/// Clamp a value to `[0.0, 1.0]`. NaN folds to `0.0`.
///
/// Centralised here so every score-mixing site uses identical semantics
/// (Fix 9 of the Module-O preparatory pass). Before this, copies in
/// `tension.rs` and `perception/metrics.rs` had drifted slightly in their
/// NaN handling.
#[inline]
pub fn clamp01(x: f32) -> f32 {
    if x.is_nan() {
        0.0
    } else {
        x.clamp(0.0, 1.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamp01_clamps() {
        assert_eq!(clamp01(-0.5), 0.0);
        assert_eq!(clamp01(0.0), 0.0);
        assert_eq!(clamp01(0.5), 0.5);
        assert_eq!(clamp01(1.0), 1.0);
        assert_eq!(clamp01(1.5), 1.0);
    }

    #[test]
    fn clamp01_nan_is_zero() {
        assert_eq!(clamp01(f32::NAN), 0.0);
    }
}
