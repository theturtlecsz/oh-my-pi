# Changelog

## [Unreleased]

### Added

- Added `revision(key, selector)`, `revisions(key)`, `receipt(receipt_id)`, and `workItems({ cursor, limit })` client methods with keyset pagination support and selector parsing.
- Added `events({ cursor, limit, afterSequence, throughSequence })` and `repositories({ cursor, limit })` client methods with keyset pagination support.
- Added `WorkRevisionListView`, `WorkItemsPage`, `DomainEventView`, `DomainEventsPage`, `RepositoryView`, `RepositoryListView`, and `EvidenceReceiptView` typed schemas.
- Added optional `repository_id` to `WorkItemView`.
- Synchronized `WORK_CONTRACT_SHA256` constant with generated contract digest.
