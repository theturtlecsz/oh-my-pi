-- Migration 0004: context identity bytes for cross-language bundle hash verification

ALTER TABLE omp_knowledge.context_bundles
    ADD COLUMN IF NOT EXISTS identity_encoding text;

ALTER TABLE omp_knowledge.context_bundles
    ADD COLUMN IF NOT EXISTS identity_canonical_json text;
