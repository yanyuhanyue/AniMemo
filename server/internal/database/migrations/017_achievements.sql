CREATE TABLE achievement_series (
 id text PRIMARY KEY, title text NOT NULL
);
CREATE TABLE achievement_tiers (
 id uuid PRIMARY KEY, series_id text NOT NULL REFERENCES achievement_series(id), tier integer NOT NULL CHECK(tier BETWEEN 1 AND 20),
 current_revision integer NOT NULL DEFAULT 1, active boolean NOT NULL DEFAULT true, archived boolean NOT NULL DEFAULT false,
 UNIQUE(series_id,tier)
);
CREATE TABLE achievement_rule_revisions (
 tier_id uuid NOT NULL REFERENCES achievement_tiers(id), revision integer NOT NULL,
 title text NOT NULL, description text NOT NULL DEFAULT '', badge text NOT NULL,
 metric text NOT NULL CHECK(metric IN ('watch_records','distinct_anime','completed_anime','watched_episodes','memory_notes')),
 threshold integer NOT NULL CHECK(threshold BETWEEN 1 AND 1000000), created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tier_id,revision)
);
CREATE TABLE achievement_progress (
 owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE, tier_id uuid NOT NULL REFERENCES achievement_tiers(id),
 rule_revision integer NOT NULL, value integer NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_id,tier_id), FOREIGN KEY(tier_id,rule_revision) REFERENCES achievement_rule_revisions(tier_id,revision)
);
CREATE TABLE achievement_unlocks (
 id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 tier_id uuid NOT NULL REFERENCES achievement_tiers(id), rule_revision integer NOT NULL,
 source text NOT NULL CHECK(source IN ('automatic','administrator')), value integer NOT NULL,
 unlocked_at timestamptz NOT NULL DEFAULT now(), UNIQUE(owner_id,tier_id), UNIQUE(id,owner_id),
 FOREIGN KEY(tier_id,rule_revision) REFERENCES achievement_rule_revisions(tier_id,revision)
);
CREATE TABLE achievement_grants (
 unlock_id uuid PRIMARY KEY REFERENCES achievement_unlocks(id) ON DELETE CASCADE,
 active boolean NOT NULL DEFAULT true, notified boolean NOT NULL DEFAULT false,
 showcase_slot integer CHECK(showcase_slot BETWEEN 1 AND 6)
);
CREATE TABLE achievement_grant_events (
 id uuid PRIMARY KEY, unlock_id uuid NOT NULL REFERENCES achievement_unlocks(id) ON DELETE CASCADE,
 action text NOT NULL CHECK(action IN ('granted','revoked')), actor_id uuid REFERENCES users(id) ON DELETE SET NULL,
 reason text NOT NULL DEFAULT '', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE achievement_backfills (
 id uuid PRIMARY KEY, actor_id uuid REFERENCES users(id) ON DELETE SET NULL,
 state text NOT NULL CHECK(state IN ('preview','running','paused','done')),
 rules jsonb NOT NULL, owners uuid[] NOT NULL, cursor integer NOT NULL DEFAULT 0,
 granted integer NOT NULL DEFAULT 0, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE achievement_pending (
 owner_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, generation bigint NOT NULL DEFAULT 1,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION enqueue_achievement_projection() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF current_setting('animemo.restore_history',true)='on' THEN RETURN NULL; END IF;
 INSERT INTO achievement_pending(owner_id) VALUES(NEW.owner_id) ON CONFLICT(owner_id) DO UPDATE SET generation=achievement_pending.generation+1,updated_at=now();
 RETURN NULL;
END $$;
CREATE TRIGGER memory_achievement_pending AFTER INSERT ON memory_revisions FOR EACH ROW EXECUTE FUNCTION enqueue_achievement_projection();
CREATE TRIGGER library_achievement_pending AFTER INSERT ON library_revisions FOR EACH ROW EXECUTE FUNCTION enqueue_achievement_projection();
INSERT INTO achievement_series VALUES ('first-steps','故事的开始'),('many-worlds','走过许多世界'),('memory-pages','记忆的页码');
INSERT INTO achievement_tiers(id,series_id,tier) VALUES
 ('a1000000-0000-4000-8000-000000000001','first-steps',1),
 ('a1000000-0000-4000-8000-000000000002','first-steps',2),
 ('a1000000-0000-4000-8000-000000000003','many-worlds',1),
 ('a1000000-0000-4000-8000-000000000004','many-worlds',2),
 ('a1000000-0000-4000-8000-000000000005','memory-pages',1);
INSERT INTO achievement_rule_revisions(tier_id,revision,title,description,badge,metric,threshold) VALUES
 ('a1000000-0000-4000-8000-000000000001',1,'第一束光','留下第一条观看记录','spark','watch_records',1),
 ('a1000000-0000-4000-8000-000000000002',1,'第十次相遇','累计留下十条观看记录','moon','watch_records',10),
 ('a1000000-0000-4000-8000-000000000003',1,'五个世界','记录过五部不同作品','orbit','distinct_anime',5),
 ('a1000000-0000-4000-8000-000000000004',1,'故事落幕','看完三部作品','flower','completed_anime',3),
 ('a1000000-0000-4000-8000-000000000005',1,'写给以后的自己','写下第一份独立记忆','book','memory_notes',1);
INSERT INTO achievement_pending(owner_id) SELECT id FROM users;
