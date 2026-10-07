CREATE TABLE import_jobs (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    format text NOT NULL CHECK (format IN ('json','csv','zip')),
    state text NOT NULL DEFAULT 'validating' CHECK (state IN ('validating','ready','applying','done','failed','cancelled')),
    source bytea,
    document jsonb,
    preview jsonb NOT NULL DEFAULT '{}',
    fingerprint text NOT NULL DEFAULT '',
    error text NOT NULL DEFAULT '',
    created integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '1 day',
    CHECK (source IS NULL OR octet_length(source) <= 167772160)
);
CREATE INDEX import_jobs_owner ON import_jobs(user_id,created_at DESC);
CREATE INDEX import_jobs_pending ON import_jobs(created_at) WHERE state IN ('validating','applying');
