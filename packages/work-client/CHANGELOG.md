# Changelog

## [Unreleased]

### Added

- Synced contract digest for record_external_delivery (OMP-283).
- Research client bindings generated from the canonical contract schemas: campaigns, trials, observations, deliverable bindings, and component compatibility (OMP-322).

### Fixed

- Stretch the loopback request abort window with host load so a busy machine no longer cancels a valid Work Ledger command mid-flight (OMP-342).
- Resend a command whose loopback exchange was interrupted before a complete response arrived (a retired keep-alive socket, or an aborted body read under load), so it no longer surfaces as an unavailable Work Ledger (OMP-342).
