# Changelog

## [Unreleased]

### Added

- Add research domain types, campaign/trial/observation/deliverable-binding models, command payloads, and `research(key)` query method on `WorkClient` (R02-S1).
- Added `budget_resource` on `ProviderAccount`, `resource` and `scope_id` on `BudgetQuote` (legacy nullable compatible), and updated `WORK_CONTRACT_SHA256` digest (OMP-233).
- Added `quote_budget` command and `BudgetQuoteResult` type, and optional `quote_id` on `ReserveBudgetPayload` (OMP-233).

- Added `rateCards()` and `rateCard(rateCardId)` query methods and `register_rate_card` command execution for immutable rate-card authority.
- Added `revision(key, selector)`, `revisions(key)`, `receipt(receipt_id)`, and `workItems({ cursor, limit })` client methods with keyset pagination support and selector parsing.
- Added `events({ cursor, limit, afterSequence, throughSequence })` and `repositories({ cursor, limit })` client methods with keyset pagination support.
- Added `WorkRevisionListView`, `WorkItemsPage`, `DomainEventView`, `DomainEventsPage`, `RepositoryView`, `RepositoryListView`, and `EvidenceReceiptView` typed schemas.
- Added optional `repository_id` to `WorkItemView`.
- Synchronized `WORK_CONTRACT_SHA256` constant with generated contract digest.
