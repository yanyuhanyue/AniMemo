ALTER TABLE entries ALTER COLUMN score TYPE numeric(3,1);
ALTER TABLE entries ADD COLUMN details jsonb NOT NULL DEFAULT '{}';
ALTER TABLE entries ADD CONSTRAINT entries_details_object CHECK (jsonb_typeof(details) = 'object');
ALTER TABLE watch_records ADD COLUMN rewatch integer NOT NULL DEFAULT 1 CHECK (rewatch BETWEEN 1 AND 1000);
ALTER TABLE watch_records ADD COLUMN version integer NOT NULL DEFAULT 1 CHECK (version > 0);

ALTER TABLE users ADD COLUMN bio text NOT NULL DEFAULT '' CHECK (char_length(bio) <= 240);
ALTER TABLE users ADD COLUMN accent text NOT NULL DEFAULT 'violet' CHECK (accent IN ('violet','coral','blue','green','amber'));
ALTER TABLE users ADD COLUMN default_view text NOT NULL DEFAULT 'cards' CHECK (default_view IN ('cards','list'));
ALTER TABLE users ADD COLUMN version integer NOT NULL DEFAULT 1 CHECK (version > 0);
ALTER TABLE users ADD COLUMN is_admin boolean NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN disabled boolean NOT NULL DEFAULT false;

CREATE TABLE avatars (
    user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    revision uuid NOT NULL UNIQUE,
    content_type text NOT NULL CHECK (content_type IN ('image/jpeg','image/png')),
    data bytea NOT NULL CHECK (octet_length(data) BETWEEN 1 AND 2097152)
);

CREATE TABLE tags (
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 24),
    color text NOT NULL CHECK (color ~ '^#[0-9a-fA-F]{6}$'),
    PRIMARY KEY (user_id,name)
);

CREATE TABLE quick_filters (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 40),
    search text NOT NULL DEFAULT '' CHECK (char_length(search) <= 160),
    status text NOT NULL DEFAULT '' CHECK (status IN ('','planned','watching','completed','on_hold','dropped')),
    sort text NOT NULL DEFAULT 'updated' CHECK (sort IN ('updated','title','score')),
    UNIQUE (user_id,name)
);
