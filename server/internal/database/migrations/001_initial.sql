CREATE TABLE users (
    id uuid PRIMARY KEY,
    email text NOT NULL UNIQUE CHECK (email = lower(email)),
    display_name text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 32),
    password_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
    token_hash bytea PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_expiry_idx ON sessions (expires_at);
CREATE INDEX sessions_user_idx ON sessions (user_id);

CREATE TABLE entries (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 160),
    original_title text NOT NULL DEFAULT '' CHECK (char_length(original_title) <= 160),
    format text NOT NULL DEFAULT 'tv' CHECK (format IN ('tv','movie','ova','other')),
    status text NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','watching','completed','on_hold','dropped')),
    total_episodes integer NOT NULL DEFAULT 0 CHECK (total_episodes BETWEEN 0 AND 10000),
    watched_episodes integer NOT NULL DEFAULT 0 CHECK (watched_episodes BETWEEN 0 AND 10000),
    score integer CHECK (score BETWEEN 1 AND 10),
    notes text NOT NULL DEFAULT '' CHECK (char_length(notes) <= 4000),
    tags text[] NOT NULL DEFAULT '{}' CHECK (cardinality(tags) <= 8),
    accent text NOT NULL DEFAULT 'violet' CHECK (accent IN ('violet','coral','blue','green','amber')),
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (total_episodes = 0 OR watched_episodes <= total_episodes)
);
CREATE INDEX entries_user_updated_idx ON entries (user_id, updated_at DESC, id);
CREATE INDEX entries_user_status_idx ON entries (user_id, status);

CREATE TABLE watch_records (
    id uuid PRIMARY KEY,
    entry_id uuid NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    watched_on date NOT NULL,
    episode_from integer NOT NULL CHECK (episode_from BETWEEN 1 AND 10000),
    episode_to integer NOT NULL CHECK (episode_to BETWEEN episode_from AND 10000),
    note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 2000),
    request_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (entry_id, request_id)
);
CREATE INDEX watch_records_entry_date_idx ON watch_records (entry_id, watched_on DESC, created_at DESC);
