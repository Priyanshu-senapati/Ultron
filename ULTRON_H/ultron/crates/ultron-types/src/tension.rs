use serde::{Deserialize, Serialize};

/// A point-in-time read of the tension subsystem.
///
/// All values normalise to roughly `0.0 ..= 1.0`. `value` is the composite
/// score that drives behaviour throttling across modules.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
pub struct TensionSnapshot {
    pub value: f32,
    pub typing_volatility: f32,
    pub click_rate: f32,
    pub idle_secs: f32,
    pub error_signal: f32, // backspace burst rate
    pub ts_unix_ms: i64,
}

impl Default for TensionSnapshot {
    fn default() -> Self {
        Self {
            value: 0.0,
            typing_volatility: 0.0,
            click_rate: 0.0,
            idle_secs: 0.0,
            error_signal: 0.0,
            ts_unix_ms: 0,
        }
    }
}

/// Coarse band labels for HUD / voice. Threshold edges are intentionally
/// asymmetric (hysteresis) — see `TensionTracker::band` in `ultron-core`.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TensionBand {
    Calm,    //  0.00 .. 0.25
    Neutral, //  0.25 .. 0.55
    Loaded,  //  0.55 .. 0.80
    Spiked,  //  0.80 .. 1.00
}

impl TensionBand {
    pub fn from_value(v: f32) -> Self {
        if v < 0.25 {
            Self::Calm
        } else if v < 0.55 {
            Self::Neutral
        } else if v < 0.80 {
            Self::Loaded
        } else {
            Self::Spiked
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn band_edges() {
        assert_eq!(TensionBand::from_value(0.0), TensionBand::Calm);
        assert_eq!(TensionBand::from_value(0.249), TensionBand::Calm);
        assert_eq!(TensionBand::from_value(0.25), TensionBand::Neutral);
        assert_eq!(TensionBand::from_value(0.79), TensionBand::Loaded);
        assert_eq!(TensionBand::from_value(0.80), TensionBand::Spiked);
        assert_eq!(TensionBand::from_value(1.0), TensionBand::Spiked);
    }
}
