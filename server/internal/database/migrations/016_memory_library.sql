-- Personal identities and documents remain Core-owned, independent of providers.
CREATE TABLE characters (
 id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 name text NOT NULL, aliases text[] NOT NULL DEFAULT '{}', description text NOT NULL DEFAULT '',
 anime_ids uuid[] NOT NULL DEFAULT '{}', favorite boolean NOT NULL DEFAULT false,
 visibility text NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','unlisted','public')),
 redirect_id uuid, version integer NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(id,owner_id), FOREIGN KEY(redirect_id,owner_id) REFERENCES characters(id,owner_id), CHECK(id<>redirect_id)
);
CREATE INDEX characters_owner ON characters(owner_id,updated_at DESC,id);
CREATE TABLE episodes (
 id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 anime_id uuid NOT NULL, title text NOT NULL, number integer NOT NULL CHECK(number BETWEEN 0 AND 100000),
 kind text NOT NULL CHECK(kind IN ('main','special','ova','movie')),
 progress_role text NOT NULL CHECK(progress_role IN ('required','optional','excluded')),
 identities jsonb NOT NULL DEFAULT '[]', redirect_id uuid, version integer NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(id,owner_id), FOREIGN KEY(anime_id,owner_id) REFERENCES anime_resources(id,owner_id),
 FOREIGN KEY(redirect_id,owner_id) REFERENCES episodes(id,owner_id), CHECK(id<>redirect_id)
);
CREATE INDEX episodes_anime ON episodes(owner_id,anime_id,number,id);
CREATE TABLE anime_relations (
 owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 from_id uuid NOT NULL, to_id uuid NOT NULL, relation text NOT NULL CHECK(relation IN ('sequel','prequel','side_story','same_franchise')),
 PRIMARY KEY(owner_id,from_id,to_id,relation), CHECK(from_id<>to_id),
 FOREIGN KEY(from_id,owner_id) REFERENCES anime_resources(id,owner_id), FOREIGN KEY(to_id,owner_id) REFERENCES anime_resources(id,owner_id)
);
CREATE TABLE progress_assertions (
 id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 anime_id uuid NOT NULL, scope text NOT NULL CHECK(scope IN ('mainline','all','custom')),
 precision text NOT NULL CHECK(precision IN ('exact','approximate','caught_up')),
 episode_ids uuid[] NOT NULL, note text NOT NULL DEFAULT '', created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(anime_id,owner_id) REFERENCES anime_resources(id,owner_id)
);
CREATE INDEX progress_assertions_anime ON progress_assertions(owner_id,anime_id,created_at DESC);
CREATE TABLE private_memory_media (
 id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 state text NOT NULL CHECK(state IN ('reserved','ready','deleted')),
 content_type text NOT NULL DEFAULT '', sha256 text NOT NULL DEFAULT '', byte_size integer NOT NULL DEFAULT 0,
 width integer NOT NULL DEFAULT 0, height integer NOT NULL DEFAULT 0,
 data bytea, thumbnail bytea, created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz, deleted_at timestamptz, UNIQUE(id,owner_id),
 CHECK(state<>'ready' OR (data IS NOT NULL AND thumbnail IS NOT NULL AND byte_size>0)),
 CHECK(state<>'deleted' OR (data IS NULL AND thumbnail IS NULL))
);
CREATE INDEX private_media_owner ON private_memory_media(owner_id,state);
CREATE TABLE memory_notes (
 id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 kind text NOT NULL DEFAULT 'note' CHECK(kind IN ('note','moment')),
 title text NOT NULL, body text NOT NULL DEFAULT '', anime_id uuid, character_id uuid, episode_id uuid, watch_id uuid,
 anchor jsonb NOT NULL DEFAULT '{}', media_ids uuid[] NOT NULL DEFAULT '{}', tags text[] NOT NULL DEFAULT '{}',
 occurred_on text NOT NULL DEFAULT '', time_precision text NOT NULL DEFAULT 'unknown' CHECK(time_precision IN ('day','month','year','approximate','unknown')),
 visibility text NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','unlisted','public')),
 spoiler boolean NOT NULL DEFAULT false, highlight boolean NOT NULL DEFAULT false,
 version integer NOT NULL DEFAULT 1, deleted_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(id,owner_id), FOREIGN KEY(anime_id,owner_id) REFERENCES anime_resources(id,owner_id),
 FOREIGN KEY(character_id,owner_id) REFERENCES characters(id,owner_id), FOREIGN KEY(episode_id,owner_id) REFERENCES episodes(id,owner_id)
);
CREATE INDEX memory_notes_owner ON memory_notes(owner_id,updated_at DESC,id) WHERE deleted_at IS NULL;
CREATE INDEX memory_notes_text ON memory_notes USING gin(to_tsvector('simple',title||' '||body));
CREATE TABLE memory_collections (
 id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 title text NOT NULL, description text NOT NULL DEFAULT '', items jsonb NOT NULL DEFAULT '[]',
 visibility text NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','unlisted','public')),
 version integer NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(id,owner_id)
);
CREATE TABLE library_revisions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 resource_id uuid NOT NULL, kind text NOT NULL, action text NOT NULL, snapshot jsonb NOT NULL,
 recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX library_revisions_owner ON library_revisions(owner_id,resource_id,recorded_at DESC,id);
CREATE TABLE yearly_memories (
 id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 year integer NOT NULL CHECK(year BETWEEN 1900 AND 2100), timezone text NOT NULL,
 title text NOT NULL, introduction text NOT NULL DEFAULT '', version integer NOT NULL DEFAULT 1,
 visibility text NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','unlisted','public')),
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,owner_id)
);
CREATE TABLE yearly_revisions (
 id uuid PRIMARY KEY, yearly_id uuid NOT NULL REFERENCES yearly_memories(id) ON DELETE CASCADE,
 revision integer NOT NULL, cutoff timestamptz NOT NULL, algorithm text NOT NULL,
 title text NOT NULL, introduction text NOT NULL, stats jsonb NOT NULL, items jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(yearly_id,revision)
);
CREATE TABLE memory_share_tokens (
 token_hash text PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 resource_id uuid NOT NULL, kind text NOT NULL CHECK(kind IN ('note','collection','yearly','character')),
 expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(owner_id,kind,resource_id)
);
-- An imported library is restored atomically; all references are remapped.
ALTER TABLE import_jobs ADD COLUMN selection jsonb;

-- Only an explicitly configured, eligible public owner can supply the site home.
ALTER TABLE site_settings ADD COLUMN homepage_owner uuid REFERENCES users(id) ON DELETE SET NULL;
