-- Site branding is public and small enough to travel with the database backup.
CREATE TABLE site_images (
    kind text PRIMARY KEY CHECK (kind IN ('icon', 'cover')),
    revision uuid NOT NULL,
    content_type text NOT NULL CHECK (content_type IN ('image/jpeg', 'image/png')),
    data bytea NOT NULL CHECK (octet_length(data) BETWEEN 1 AND 2097152)
);
