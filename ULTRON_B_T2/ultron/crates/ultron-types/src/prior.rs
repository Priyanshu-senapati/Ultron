//! Productivity-prior update payloads (Phase 1, Module D → Module O).
//!
//! Module D learns a per-hour productivity curve from accumulated history,
//! and publishes a [`ProductivityPriorUpdate`] every 10–15 minutes (or on
//! significant change). Module O subscribes to these and overrides its
//! default [`crate::insight::CircadianPhase::default_productivity_prior`]
//! table with the learned values.
//!
//! ## Wire format
//!
//! Published as an [`crate::events::UltronEvent::Custom`] with
//! `kind = "productivity_prior_update"` and the serialised
//! `ProductivityPriorUpdate` as its payload. We use the Custom envelope
//! rather than a first-class enum variant so:
//!
//! 1. The core daemon's WebSocket bridge surfaces the event unchanged,
//!    with no new match arm in `explode_event`.
//! 2. Consumers can decode strictly via `serde_json::from_value::<...>`,
//!    getting full typechecking despite the on-the-wire generality.
//!
//! ## Stability
//!
//! Schema versioning is intentional. `schema_version` starts at 1; any
//! breaking change bumps it so consumers can refuse stale formats.

use serde::{Deserialize, Serialize};

/// Schema version of the current `ProductivityPriorUpdate` definition.
pub const PRIOR_SCHEMA_VERSION: u32 = 1;

/// Per-hour productivity prior, learned by Module D from accumulated
/// `InsightSnapshot` history. Replaces Module O's static
/// `CircadianPhase::default_productivity_prior` defaults.
///
/// `priors[h]` is the prior for local hour `h` in `0..=23`, in
/// `[0.0, 1.0]`. Missing data → fall back to circadian default.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ProductivityPriorUpdate {
    /// Local-hour productivity priors. `[0.0, 1.0]`. Indexed 0–23.
    /// `None` for an hour means "no data yet — keep using the default".
    pub priors: [Option<f32>; 24],

    /// Sample count per hour. Lets consumers gauge confidence. Hours
    /// below `min_samples_for_use` (a consumer-side threshold) can be
    /// treated as "not yet learned".
    pub sample_counts: [u32; 24],

    /// When this update was produced, Unix epoch ms.
    pub ts_unix_ms: i64,

    /// Schema revision. Receivers refuse / downgrade on mismatch.
    pub schema_version: u32,
}

impl ProductivityPriorUpdate {
    /// Wire `kind` value for this event. Centralised so producers and
    /// consumers agree on the string.
    pub const KIND: &'static str = "productivity_prior_update";

    /// Empty update with no learned data. Useful as a default or
    /// sentinel.
    pub fn empty(now_ms: i64) -> Self {
        Self {
            priors: [None; 24],
            sample_counts: [0; 24],
            ts_unix_ms: now_ms,
            schema_version: PRIOR_SCHEMA_VERSION,
        }
    }

    /// `true` if at least one hour has any data.
    pub fn has_any_data(&self) -> bool {
        self.priors.iter().any(|p| p.is_some())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_has_no_data() {
        let p = ProductivityPriorUpdate::empty(0);
        assert!(!p.has_any_data());
        assert_eq!(p.schema_version, PRIOR_SCHEMA_VERSION);
    }

    #[test]
    fn roundtrip_serde() {
        let mut p = ProductivityPriorUpdate::empty(123);
        p.priors[9] = Some(0.85);
        p.sample_counts[9] = 42;
        let v = serde_json::to_string(&p).unwrap();
        let back: ProductivityPriorUpdate = serde_json::from_str(&v).unwrap();
        assert_eq!(p, back);
        assert!(back.has_any_data());
        assert_eq!(back.priors[9], Some(0.85));
    }

    #[test]
    fn kind_constant_matches_snake_case() {
        assert_eq!(ProductivityPriorUpdate::KIND, "productivity_prior_update");
    }
}
