ALTER TABLE entry_covers ALTER COLUMN data DROP NOT NULL;
ALTER TABLE avatars ALTER COLUMN data DROP NOT NULL;
ALTER TABLE column_covers ALTER COLUMN data DROP NOT NULL;
ALTER TABLE avatars ADD COLUMN byte_size integer;
UPDATE avatars SET byte_size=octet_length(data);
ALTER TABLE avatars ALTER COLUMN byte_size SET NOT NULL;
ALTER TABLE avatars ADD CHECK(byte_size BETWEEN 1 AND 2097152);
ALTER TABLE column_covers ADD COLUMN byte_size integer;
UPDATE column_covers SET byte_size=octet_length(data);
ALTER TABLE column_covers ALTER COLUMN byte_size SET NOT NULL;
ALTER TABLE column_covers ADD CHECK(byte_size BETWEEN 1 AND 2097152);

CREATE VIEW media_references AS
    SELECT 'entry'::text AS kind,e.user_id AS owner_id,c.entry_id AS target_id,c.revision,c.content_type,c.byte_size,c.data FROM entry_covers c JOIN entries e ON e.id=c.entry_id
    UNION ALL SELECT 'avatar',c.user_id,c.user_id,c.revision,c.content_type,c.byte_size,c.data FROM avatars c
    UNION ALL SELECT 'column',e.user_id,c.column_id,c.revision,c.content_type,c.byte_size,c.data FROM column_covers c JOIN columns e ON e.id=c.column_id;

CREATE TABLE media_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    backend text NOT NULL DEFAULT 'postgres' CHECK(backend IN ('postgres','r2')),
    backend_id text NOT NULL DEFAULT '',
    namespace uuid NOT NULL DEFAULT gen_random_uuid(),
    version integer NOT NULL DEFAULT 1
);
INSERT INTO media_settings(singleton) VALUES(true);
CREATE TABLE media_objects (
    revision uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    kind text NOT NULL CHECK(kind IN ('entry','avatar','column','probe')),
    target_id uuid NOT NULL,
    backend_id text NOT NULL,
    object_key text NOT NULL,
    content_type text NOT NULL,
    byte_size integer NOT NULL CHECK(byte_size BETWEEN 1 AND 2097152),
    sha256 text NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
    state text NOT NULL CHECK(state IN ('uploading','stored','orphan','deleted')),
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    error text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX media_objects_pending ON media_objects(available_at) WHERE state IN ('uploading','orphan');
CREATE TABLE media_migrations (
    id uuid PRIMARY KEY,
    backend text NOT NULL CHECK(backend IN ('postgres','r2')),
    state text NOT NULL DEFAULT 'running' CHECK(state IN ('running','done','superseded')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE media_migration_items (
    migration_id uuid NOT NULL REFERENCES media_migrations(id) ON DELETE CASCADE,
    revision uuid NOT NULL,
    kind text NOT NULL,
    target_id uuid NOT NULL,
    owner_id uuid NOT NULL,
    content_type text NOT NULL,
    byte_size integer NOT NULL,
    sha256 text NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','copied','superseded')),
    original_data bytea,
    PRIMARY KEY(migration_id,revision)
);

CREATE FUNCTION retire_media_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR OLD.revision<>NEW.revision THEN
        UPDATE media_objects SET state='orphan',available_at=now(),updated_at=now() WHERE revision=OLD.revision AND state<>'deleted';
        UPDATE media_migration_items SET state='superseded',original_data=NULL WHERE revision=OLD.revision;
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER entry_cover_retirement AFTER UPDATE OR DELETE ON entry_covers FOR EACH ROW EXECUTE FUNCTION retire_media_revision();
CREATE TRIGGER avatar_retirement AFTER UPDATE OR DELETE ON avatars FOR EACH ROW EXECUTE FUNCTION retire_media_revision();
CREATE TRIGGER column_cover_retirement AFTER UPDATE OR DELETE ON column_covers FOR EACH ROW EXECUTE FUNCTION retire_media_revision();
