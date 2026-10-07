-- Bound public discovery scans and private trash filtering as collections grow.
CREATE INDEX entries_public_updated_idx ON entries(updated_at DESC,id) WHERE visibility='public' AND NOT moderated_hidden AND deleted_at IS NULL;
CREATE INDEX columns_public_featured_idx ON columns(featured DESC,updated_at DESC,id) WHERE state='published' AND deleted_at IS NULL;
CREATE INDEX users_public_directory_idx ON users(display_name,id) WHERE sharing_enabled AND public_state='published' AND NOT disabled;
