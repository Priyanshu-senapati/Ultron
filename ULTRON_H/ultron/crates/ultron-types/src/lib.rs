//! Shared type vocabulary for ULTRON modules.
//!
//! This crate has zero IO and no platform deps. Anything that crosses
//! a module boundary (event bus, WebSocket, Quantum Log payload) lives here.

pub mod events;
pub mod messages;
pub mod perception;
pub mod tension;

pub use events::*;
pub use messages::*;
pub use perception::*;
pub use tension::*;
