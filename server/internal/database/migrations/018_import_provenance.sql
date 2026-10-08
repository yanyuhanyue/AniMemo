ALTER TABLE watch_records ADD COLUMN source_line integer NOT NULL DEFAULT 0 CHECK(source_line BETWEEN 0 AND 1000000);
ALTER TABLE watch_records ADD COLUMN source_filename text NOT NULL DEFAULT '' CHECK(char_length(source_filename)<=255);
