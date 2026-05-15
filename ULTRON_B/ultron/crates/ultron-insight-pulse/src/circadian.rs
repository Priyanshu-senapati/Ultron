//! Circadian helper.
//!
//! The phase enum and the productivity-prior table live in `ultron-types`
//! (see `insight.rs`) so they're shared with any future consumer. This
//! module exists for two reasons:
//!
//! 1. To centralise the "what is the current circadian phase?" call so the
//!    fusion code never reaches for `chrono::Local::now()` directly.
//!    Hard-coding the clock makes unit tests deterministic.
//! 2. To document how `productivity_prior` is expected to evolve — Module
//!    D (Memory Engine, not yet built) will eventually supply a learned
//!    behavioural curve via a `productivity_prior_override` event on the
//!    bus. Until then, this is the only source of the value.

use chrono::{Local, Timelike};
use ultron_types::CircadianPhase;

/// Return the [`CircadianPhase`] for *now* in local time.
pub fn current_phase() -> CircadianPhase {
    phase_at(Local::now().hour())
}

/// Pure function: classify by hour-of-day (`0..=23`). Exposed so unit tests
/// can verify boundaries without monkey-patching the clock.
#[inline]
pub fn phase_at(hour: u32) -> CircadianPhase {
    CircadianPhase::from_hour(hour)
}

/// Phase-1 productivity prior for the current local time. A direct
/// delegate to [`CircadianPhase::default_productivity_prior`]; we wrap
/// it so future overrides (Module D's learned curve) plug in here.
pub fn current_productivity_prior() -> f32 {
    current_phase().default_productivity_prior()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn phase_at_boundaries_match_spec() {
        // Boundary points from the build prompt:
        // 08:00 → Morning, 12:00 → Afternoon, 21:00 → Night, 00:00 → LateNight.
        assert_eq!(phase_at(0), CircadianPhase::LateNight);
        assert_eq!(phase_at(8), CircadianPhase::Morning);
        assert_eq!(phase_at(12), CircadianPhase::Afternoon);
        assert_eq!(phase_at(21), CircadianPhase::Night);
    }

    #[test]
    fn current_phase_returns_valid_variant() {
        // We can't pin "now" — just make sure the call works and the
        // result is one of the six variants. (Implicit: `from_hour` is
        // total over `0..=23`.)
        let _ = current_phase();
        let prior = current_productivity_prior();
        assert!(prior > 0.0 && prior <= 1.0);
    }
}
