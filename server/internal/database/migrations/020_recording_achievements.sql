ALTER TABLE achievement_rule_revisions DROP CONSTRAINT achievement_rule_revisions_metric_check;
ALTER TABLE achievement_rule_revisions ADD CHECK(metric IN (
 'watch_records','distinct_anime','completed_anime','watched_episodes','memory_notes',
 'recorded_anime','memory_collections','yearly_albums'
));

-- Use the existing projection queue for album revisions and moderation changes.
-- Portable restoration preserves its own achievement history without recomputing it.
CREATE OR REPLACE FUNCTION enqueue_achievement_projection() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE owner uuid;
BEGIN
 IF current_setting('animemo.restore_history',true)='on' THEN RETURN NULL; END IF;
 IF TG_TABLE_NAME='yearly_revisions' THEN
   SELECT owner_id INTO owner FROM yearly_memories WHERE id=NEW.yearly_id;
 ELSIF TG_TABLE_NAME='entries' THEN
   owner=NEW.user_id;
 ELSE
   owner=NEW.owner_id;
 END IF;
 INSERT INTO achievement_pending(owner_id) VALUES(owner)
   ON CONFLICT(owner_id) DO UPDATE SET generation=achievement_pending.generation+1,updated_at=now();
 RETURN NULL;
END $$;
CREATE TRIGGER yearly_achievement_pending AFTER INSERT ON yearly_revisions
 FOR EACH ROW EXECUTE FUNCTION enqueue_achievement_projection();
CREATE TRIGGER entry_moderation_achievement_pending AFTER UPDATE OF deleted_at ON entries
 FOR EACH ROW WHEN (OLD.deleted_at IS DISTINCT FROM NEW.deleted_at)
 EXECUTE FUNCTION enqueue_achievement_projection();

INSERT INTO achievement_series VALUES
 ('recorded-worlds','记下的世界'),('memory-shelves','把回忆收好'),('yearly-albums','装订时光')
 ON CONFLICT(id) DO NOTHING;

-- Existing administrator-defined tiers take precedence; never replace their rules.
WITH seeds(id,series_id,tier,title,description,badge,metric,threshold) AS (VALUES
 ('a1000000-0000-4000-8000-000000000006'::uuid,'recorded-worlds',1,'故事有了落脚处','把第一部看过的作品记下来，只记得名字也可以','spark','recorded_anime',1),
 ('a1000000-0000-4000-8000-000000000007'::uuid,'recorded-worlds',2,'书架上的十个世界','为十部不同作品留下位置','orbit','recorded_anime',10),
 ('a1000000-0000-4000-8000-000000000008'::uuid,'memory-pages',2,'几页心事','留下五份笔记或瞬间，日期不详也值得珍藏','book','memory_notes',5),
 ('a1000000-0000-4000-8000-000000000009'::uuid,'memory-pages',3,'给未来的一叠信','收藏二十份笔记或瞬间，留给以后的自己','book','memory_notes',20),
 ('a1000000-0000-4000-8000-000000000010'::uuid,'memory-shelves',1,'给回忆一个家','在第一个收藏夹中放入喜欢的作品、角色或记忆','flower','memory_collections',1),
 ('a1000000-0000-4000-8000-000000000011'::uuid,'memory-shelves',2,'各有归处','整理三个有内容的收藏夹','flower','memory_collections',3),
 ('a1000000-0000-4000-8000-000000000012'::uuid,'yearly-albums',1,'把这一年装订好','保存第一本选入了记忆的年度册','moon','yearly_albums',1)
), added AS (
 INSERT INTO achievement_tiers(id,series_id,tier)
 SELECT id,series_id,tier FROM seeds ON CONFLICT(series_id,tier) DO NOTHING RETURNING id
)
INSERT INTO achievement_rule_revisions(tier_id,revision,title,description,badge,metric,threshold)
 SELECT s.id,1,s.title,s.description,s.badge,s.metric,s.threshold FROM seeds s JOIN added a ON a.id=s.id;

-- Evaluate existing records asynchronously; do not backdate newly earned badges.
INSERT INTO achievement_pending(owner_id) SELECT id FROM users WHERE NOT disabled
 ON CONFLICT(owner_id) DO UPDATE SET generation=achievement_pending.generation+1,updated_at=now();
