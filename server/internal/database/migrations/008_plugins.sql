-- Immutable packages and activation belong to the same database snapshot.
-- Version 1 plugins are stateless: there is no external plugin volume or KV store.
CREATE TABLE plugin_releases (
    slug text NOT NULL,
    version text NOT NULL,
    digest text NOT NULL,
    manifest jsonb NOT NULL,
    module bytea NOT NULL,
    installed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (slug, version)
);
CREATE TABLE plugin_deployments (
    slug text PRIMARY KEY,
    active_version text NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    revision bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (slug, active_version) REFERENCES plugin_releases(slug, version)
);
