ALTER TABLE users ADD COLUMN sharing_enabled boolean NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN public_slug uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE;
ALTER TABLE users ADD COLUMN public_state text NOT NULL DEFAULT 'private' CHECK (public_state IN ('private','pending','published','rejected'));
ALTER TABLE users ADD COLUMN public_reason text NOT NULL DEFAULT '';
ALTER TABLE entries ADD COLUMN visibility text NOT NULL DEFAULT 'private' CHECK (visibility IN ('private','unlisted','public'));
ALTER TABLE entries ADD COLUMN share_slug uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE;
ALTER TABLE entries ADD COLUMN moderated_hidden boolean NOT NULL DEFAULT false;
ALTER TABLE entries ADD COLUMN deleted_at timestamptz;

CREATE TABLE columns (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    summary text NOT NULL DEFAULT '' CHECK (char_length(summary) <= 400),
    body text NOT NULL DEFAULT '' CHECK (char_length(body) <= 30000),
    state text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','pending','published','rejected','withdrawal_requested')),
    featured boolean NOT NULL DEFAULT false,
    reason text NOT NULL DEFAULT '',
    version integer NOT NULL DEFAULT 1,
    deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX columns_owner ON columns(user_id,updated_at DESC);
CREATE TABLE column_entries (
    column_id uuid NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
    entry_id uuid NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    PRIMARY KEY (column_id,entry_id)
);
CREATE TABLE column_covers (
    column_id uuid PRIMARY KEY REFERENCES columns(id) ON DELETE CASCADE,
    revision uuid NOT NULL UNIQUE,
    content_type text NOT NULL CHECK (content_type IN ('image/jpeg','image/png')),
    data bytea NOT NULL CHECK (octet_length(data) BETWEEN 1 AND 2097152)
);

CREATE TABLE site_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    name text NOT NULL DEFAULT 'AniMemo',
    description text NOT NULL DEFAULT '把每一次与动画相遇认真收藏。',
    registration_open boolean NOT NULL DEFAULT true,
    version integer NOT NULL DEFAULT 1
);
INSERT INTO site_settings(singleton) VALUES(true);
CREATE TABLE audit_log (
    id uuid PRIMARY KEY,
    actor_id uuid REFERENCES users(id) ON DELETE SET NULL,
    action text NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_date ON audit_log(created_at DESC,id);
