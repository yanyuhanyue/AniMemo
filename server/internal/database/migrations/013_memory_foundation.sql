-- Core identities outlive provider bindings and individual journal entries.
CREATE TABLE anime_resources (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(id,owner_id)
);
ALTER TABLE entries ADD COLUMN anime_id uuid;
INSERT INTO anime_resources(id,owner_id,title) SELECT id,user_id,title FROM entries;
UPDATE entries SET anime_id=id;
ALTER TABLE entries ALTER COLUMN anime_id SET NOT NULL;
ALTER TABLE entries ADD FOREIGN KEY(anime_id,user_id) REFERENCES anime_resources(id,owner_id);
CREATE FUNCTION assign_anime_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.anime_id IS NULL THEN
        INSERT INTO anime_resources(owner_id,title) VALUES(NEW.user_id,NEW.title) RETURNING id INTO NEW.anime_id;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER entry_identity BEFORE INSERT ON entries FOR EACH ROW EXECUTE FUNCTION assign_anime_identity();

CREATE TABLE anime_external_identities (
    anime_id uuid NOT NULL REFERENCES anime_resources(id) ON DELETE CASCADE,
    provider text NOT NULL,
    external_id text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    metadata jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(anime_id,provider,external_id)
);
INSERT INTO anime_external_identities SELECT e.anime_id,s.provider,s.subject_id::text,true,s.metadata,s.refreshed_at FROM entry_sources s JOIN entries e ON e.id=s.entry_id;
CREATE FUNCTION remember_provider_identity() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE resource uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        SELECT anime_id INTO resource FROM entries WHERE id=OLD.entry_id;
        UPDATE anime_external_identities SET active=false WHERE anime_id=resource AND provider=OLD.provider;
        RETURN OLD;
    END IF;
    SELECT anime_id INTO resource FROM entries WHERE id=NEW.entry_id;
    UPDATE anime_external_identities SET active=false WHERE anime_id=resource AND provider=NEW.provider;
    INSERT INTO anime_external_identities(anime_id,provider,external_id,metadata) VALUES(resource,NEW.provider,NEW.subject_id::text,NEW.metadata)
      ON CONFLICT(anime_id,provider,external_id) DO UPDATE SET active=true,metadata=excluded.metadata,recorded_at=now();
    RETURN NEW;
END $$;
CREATE TRIGGER source_identity AFTER INSERT OR UPDATE OR DELETE ON entry_sources FOR EACH ROW EXECUTE FUNCTION remember_provider_identity();

ALTER TABLE entries ADD COLUMN airing_state text NOT NULL DEFAULT 'finished' CHECK(airing_state IN ('unknown','airing','finished'));
ALTER TABLE entries DROP CONSTRAINT entries_status_check;
ALTER TABLE entries ADD CHECK(status IN ('planned','watching','caught_up','completed','on_hold','dropped'));

-- A date's precision is part of the fact, never inferred from a padded date.
ALTER TABLE watch_records ALTER COLUMN watched_on DROP NOT NULL;
ALTER TABLE watch_records ADD COLUMN time_precision text NOT NULL DEFAULT 'day' CHECK(time_precision IN ('day','month','year','unknown'));
ALTER TABLE watch_records ADD CHECK((time_precision='unknown')=(watched_on IS NULL));
ALTER TABLE watch_records ADD CHECK(time_precision<>'month' OR extract(day FROM watched_on)=1);
ALTER TABLE watch_records ADD CHECK(time_precision<>'year' OR (extract(month FROM watched_on)=1 AND extract(day FROM watched_on)=1));

CREATE TABLE memory_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    anime_id uuid NOT NULL REFERENCES anime_resources(id) ON DELETE CASCADE,
    entry_id uuid NOT NULL,
    kind text NOT NULL CHECK(kind IN ('created','changed','removed','watch.created','watch.corrected','watch.retracted')),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    snapshot jsonb NOT NULL
);
CREATE INDEX memory_revisions_entry ON memory_revisions(owner_id,entry_id,recorded_at DESC,id);
CREATE FUNCTION preserve_entry_revision() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE fact jsonb; resource uuid; owner uuid; entry uuid; action text;
BEGIN
    IF current_setting('animemo.restore_history',true)='on' THEN RETURN NULL; END IF;
    IF TG_OP='UPDATE' AND ROW(NEW.status,NEW.score,NEW.notes,NEW.watched_episodes,NEW.airing_state) IS NOT DISTINCT FROM ROW(OLD.status,OLD.score,OLD.notes,OLD.watched_episodes,OLD.airing_state) THEN RETURN NEW; END IF;
    IF TG_OP='DELETE' THEN
        fact=to_jsonb(OLD); resource=OLD.anime_id; owner=OLD.user_id; entry=OLD.id; action='removed';
    ELSE
        fact=to_jsonb(NEW); resource=NEW.anime_id; owner=NEW.user_id; entry=NEW.id; action=CASE WHEN TG_OP='INSERT' THEN 'created' ELSE 'changed' END;
    END IF;
    -- Account deletion intentionally deletes all personal data.
    IF EXISTS(SELECT 1 FROM users WHERE id=owner) THEN
        INSERT INTO memory_revisions(owner_id,anime_id,entry_id,kind,snapshot) VALUES(owner,resource,entry,action,jsonb_build_object('title',fact->'title','status',fact->'status','score',fact->'score','notes',fact->'notes','watched_episodes',fact->'watched_episodes','airing_state',fact->'airing_state'));
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER entry_memory_revision AFTER INSERT OR UPDATE OR DELETE ON entries FOR EACH ROW EXECUTE FUNCTION preserve_entry_revision();
CREATE FUNCTION preserve_watch_fact() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE fact jsonb; resource uuid; owner uuid; entry uuid;
BEGIN
    IF current_setting('animemo.restore_history',true)='on' THEN RETURN NULL; END IF;
    fact=CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
    entry=(fact->>'entry_id')::uuid;
    SELECT anime_id,user_id INTO resource,owner FROM entries WHERE id=entry;
    IF owner IS NOT NULL THEN
        INSERT INTO memory_revisions(owner_id,anime_id,entry_id,kind,snapshot) VALUES(owner,resource,entry,CASE TG_OP WHEN 'INSERT' THEN 'watch.created' WHEN 'UPDATE' THEN 'watch.corrected' ELSE 'watch.retracted' END,fact);
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER watch_fact AFTER INSERT OR UPDATE OR DELETE ON watch_records FOR EACH ROW EXECUTE FUNCTION preserve_watch_fact();
