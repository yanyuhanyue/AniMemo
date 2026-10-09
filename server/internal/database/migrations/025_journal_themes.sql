-- Preserve existing notes preferences while adding another private surface.
ALTER TABLE user_note_themes RENAME TO user_theme_selections;
ALTER TABLE user_theme_selections ADD COLUMN scope text NOT NULL DEFAULT 'private.notes'
 CHECK (scope IN ('private.notes','private.journal'));
ALTER TABLE user_theme_selections DROP CONSTRAINT user_note_themes_pkey;
ALTER TABLE user_theme_selections ADD PRIMARY KEY (user_id,scope);

-- Removing package bytes must not allow a known version to acquire a new identity.
CREATE TABLE plugin_release_identities (
 slug text NOT NULL,
 version text NOT NULL,
 digest text NOT NULL,
 PRIMARY KEY (slug,version)
);
INSERT INTO plugin_release_identities SELECT slug,version,digest FROM plugin_releases;
