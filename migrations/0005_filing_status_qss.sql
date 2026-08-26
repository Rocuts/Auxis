-- Phase 2a defect fix: documents 02 and 04 both carry "Qualifying surviving
-- spouse" rows, but the 0003 CHECK admitted only four filing statuses, which
-- made two legitimate records unpersistable (found by the accuracy harness's
-- oracle load, pinned by a strict-xfail test until this migration landed).
-- 0003 is already applied everywhere, so the CHECK is replaced here rather
-- than edited in place.
ALTER TABLE records
    DROP CONSTRAINT records_filing_status_check;

ALTER TABLE records
    ADD CONSTRAINT records_filing_status_check CHECK (filing_status IN (
        'single',
        'married_filing_jointly',
        'married_filing_separately',
        'head_of_household',
        'qualifying_surviving_spouse'));
