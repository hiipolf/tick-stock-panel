//! `tf-rs-shared` — workspace-shared re-exports and helpers.
//!
//! ADR-0002: keeps `indicators` / (future) `factors` / `signals` aligned
//! on the same Polars + PyO3 surfaces without each member pinning its
//! own version. Currently a thin re-export layer; future iteration
//! will add tracing init + Polars error-context wrappers.

pub use polars;
pub use pyo3;
pub use pyo3_polars;
