-- Theme assets travel with the immutable package and ordinary database backups.
ALTER TABLE plugin_releases ADD COLUMN assets jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(assets)='object');
