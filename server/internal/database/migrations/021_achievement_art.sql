CREATE TABLE achievement_images (
 id text PRIMARY KEY CHECK(id ~ '^[a-f0-9]{64}$'),
 data bytea NOT NULL CHECK(octet_length(data) BETWEEN 1 AND 300000),
 width integer NOT NULL CHECK(width BETWEEN 1 AND 256),
 height integer NOT NULL CHECK(height BETWEEN 1 AND 256),
 uploaded_by uuid REFERENCES users(id) ON DELETE SET NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE achievement_rule_revisions ADD COLUMN badge_image_id text REFERENCES achievement_images(id);

-- Give recording, collections and annual albums distinct artwork. Preserve
-- customized rules and the revisions referenced by existing unlocks.
WITH art(id,old_badge,new_badge,metric,threshold) AS (VALUES
 ('a1000000-0000-4000-8000-000000000002'::uuid,'moon','spark','watch_records',10),
 ('a1000000-0000-4000-8000-000000000006'::uuid,'spark','ticket','recorded_anime',1),
 ('a1000000-0000-4000-8000-000000000007'::uuid,'orbit','ticket','recorded_anime',10),
 ('a1000000-0000-4000-8000-000000000010'::uuid,'flower','shelf','memory_collections',1),
 ('a1000000-0000-4000-8000-000000000011'::uuid,'flower','shelf','memory_collections',3),
 ('a1000000-0000-4000-8000-000000000012'::uuid,'moon','album','yearly_albums',1)
), added AS (
 INSERT INTO achievement_rule_revisions(tier_id,revision,title,description,badge,metric,threshold)
 SELECT r.tier_id,2,r.title,r.description,a.new_badge,r.metric,r.threshold
 FROM art a JOIN achievement_tiers t ON t.id=a.id
 JOIN achievement_rule_revisions r ON r.tier_id=t.id AND r.revision=t.current_revision
 WHERE t.current_revision=1 AND r.badge=a.old_badge AND r.metric=a.metric AND r.threshold=a.threshold
 RETURNING tier_id
)
UPDATE achievement_tiers SET current_revision=2 WHERE id IN (SELECT tier_id FROM added);
