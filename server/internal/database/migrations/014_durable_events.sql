-- Outbox insertion is in the business transaction. Delivery is at least once.
CREATE TABLE core_outbox (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind text NOT NULL,
    request_id text NOT NULL DEFAULT '',
    state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','done','failed')),
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_token uuid,
    lease_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX core_outbox_pending ON core_outbox(available_at) WHERE state IN ('pending','running');
CREATE TABLE job_attempts (
    job_id uuid NOT NULL REFERENCES core_outbox(id) ON DELETE CASCADE,
    attempt integer NOT NULL,
    lease_token uuid NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    outcome text NOT NULL DEFAULT 'running',
    PRIMARY KEY(job_id,attempt)
);
CREATE TABLE memory_activity (
    revision_id uuid PRIMARY KEY REFERENCES memory_revisions(id) ON DELETE CASCADE,
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    delivered_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION enqueue_memory_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO core_outbox(id,owner_id,kind,request_id) VALUES(NEW.id,NEW.owner_id,'memory.revision',coalesce(current_setting('animemo.request_id',true),''));
    RETURN NULL;
END $$;
CREATE TRIGGER memory_outbox AFTER INSERT ON memory_revisions FOR EACH ROW EXECUTE FUNCTION enqueue_memory_event();
CREATE TABLE worker_heartbeats (
    id text PRIMARY KEY,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE import_jobs ADD COLUMN request_id text NOT NULL DEFAULT '';
