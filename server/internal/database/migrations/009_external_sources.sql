CREATE UNIQUE INDEX entries_owner_identity ON entries(id,user_id);
CREATE TABLE entry_sources (
    entry_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider text NOT NULL CHECK (provider='bangumi'),
    subject_id bigint NOT NULL CHECK (subject_id BETWEEN 1 AND 2147483647),
    metadata jsonb NOT NULL,
    refreshed_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(entry_id,user_id) REFERENCES entries(id,user_id) ON DELETE CASCADE,
    UNIQUE(user_id,provider,subject_id)
);
