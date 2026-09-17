-- Migration 0005: exact content canonical bytes for cross-language bundle injection.
-- Rows compiled before this column exists keep NULL and are unverifiable by consumers.

ALTER TABLE omp_knowledge.context_bundles
    ADD COLUMN IF NOT EXISTS content_canonical_json text;
