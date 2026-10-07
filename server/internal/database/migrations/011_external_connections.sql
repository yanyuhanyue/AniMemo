CREATE TABLE external_connections (
    user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    generation uuid NOT NULL,
    state text NOT NULL DEFAULT 'disconnected' CHECK(state IN ('disconnected','authorizing','connected','reauthorize')),
    remote_user_id bigint,
    username text NOT NULL DEFAULT '',
    nickname text NOT NULL DEFAULT '',
    tokens bytea,
    expires_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE external_oauth_states (
    state_hash bytea PRIMARY KEY CHECK(octet_length(state_hash)=32),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_hash bytea NOT NULL CHECK(octet_length(session_hash)=32),
    generation uuid NOT NULL,
    redirect_uri text NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX external_oauth_states_owner ON external_oauth_states(user_id);

CREATE TABLE external_sync_jobs (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    remote_user_id bigint NOT NULL,
    generation uuid NOT NULL,
    request_id uuid NOT NULL,
    mode text NOT NULL CHECK(mode IN ('pull','push','two_way')),
    include_progress boolean NOT NULL DEFAULT false,
    state text NOT NULL DEFAULT 'fetching' CHECK(state IN ('fetching','ready','applying','done','failed','cancelled')),
    cursor integer NOT NULL DEFAULT 0,
    remote_total integer NOT NULL DEFAULT 0,
    error text NOT NULL DEFAULT '',
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now()+interval '30 minutes',
    UNIQUE(user_id,request_id)
);
CREATE INDEX external_sync_jobs_pending ON external_sync_jobs(created_at) WHERE state IN ('fetching','applying');
CREATE TABLE external_sync_items (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES external_sync_jobs(id) ON DELETE CASCADE,
    subject_id bigint NOT NULL,
    data jsonb NOT NULL,
    action text NOT NULL DEFAULT 'skip' CHECK(action IN ('pull','push','skip')),
    state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','done','conflict','failed','skipped')),
    result text NOT NULL DEFAULT '',
    attempts integer NOT NULL DEFAULT 0,
    write_started boolean NOT NULL DEFAULT false,
    available_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(job_id,subject_id)
);
CREATE TABLE source_sync_baselines (
    entry_id uuid NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    remote_user_id bigint NOT NULL,
    local_value jsonb NOT NULL,
    remote_value jsonb NOT NULL,
    episode_hash text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(entry_id,remote_user_id)
);
CREATE TABLE source_sync_receipts (
    change_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    request_hash bytea NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
