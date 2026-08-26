-- btree_gist lets one GiST index mix scalar equality (text, integer) with
-- range overlap — the combination the bracket exclusion constraint in
-- 0003_records.sql depends on. Supported by Neon; CREATE EXTENSION here is
-- part of the Phase 1 gate on the Neon branch.
CREATE EXTENSION IF NOT EXISTS btree_gist;
