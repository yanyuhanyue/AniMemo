-- A remembered work does not imply a known completion state, date or episode.
ALTER TABLE entries DROP CONSTRAINT entries_status_check;
ALTER TABLE entries ADD CONSTRAINT entries_status_check
    CHECK (status IN ('recorded','planned','watching','caught_up','completed','on_hold','dropped'));
ALTER TABLE entries ALTER COLUMN status SET DEFAULT 'recorded';

ALTER TABLE quick_filters DROP CONSTRAINT quick_filters_status_check;
ALTER TABLE quick_filters ADD CONSTRAINT quick_filters_status_check
    CHECK (status IN ('','recorded','planned','watching','caught_up','completed','on_hold','dropped'));
