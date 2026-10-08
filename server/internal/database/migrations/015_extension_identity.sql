ALTER TABLE plugin_releases ADD COLUMN publisher_id text NOT NULL DEFAULT 'local.reviewed';
ALTER TABLE plugin_releases ADD COLUMN distribution text NOT NULL DEFAULT 'local' CHECK(distribution IN ('local','bundled'));
ALTER TABLE plugin_deployments ADD COLUMN installation_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE;
ALTER TABLE plugin_deployments ADD COLUMN health text NOT NULL DEFAULT 'ready' CHECK(health IN ('ready','quarantined','review_required'));
ALTER TABLE plugin_deployments ADD COLUMN health_reason text NOT NULL DEFAULT '';
CREATE TABLE bundled_extensions (
    slug text PRIMARY KEY,
    version text NOT NULL,
    digest text NOT NULL,
    core_sha256 text NOT NULL
);

CREATE TABLE plugin_invocations (
    id uuid PRIMARY KEY,
    actor_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    installation_id uuid NOT NULL,
    package_digest text NOT NULL,
    capability text NOT NULL CHECK(capability='import.convert'),
    outcome text NOT NULL DEFAULT 'running' CHECK(outcome IN ('running','done','failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
