-- A personal appearance preference, separate from the administrator's activation.
-- Packages and preferences are included in the existing full database backup.
CREATE TABLE user_note_themes (
    user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    slug text NOT NULL REFERENCES plugin_deployments(slug) ON DELETE CASCADE
);
