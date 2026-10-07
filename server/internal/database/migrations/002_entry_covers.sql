CREATE TABLE entry_covers (
    entry_id uuid PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
    revision uuid NOT NULL UNIQUE,
    content_type text NOT NULL CHECK (content_type IN ('image/jpeg', 'image/png')),
    width integer NOT NULL CHECK (width BETWEEN 1 AND 8192),
    height integer NOT NULL CHECK (height BETWEEN 1 AND 8192),
    byte_size integer NOT NULL CHECK (byte_size BETWEEN 1 AND 2097152),
    data bytea NOT NULL,
    CHECK (width * height <= 12000000),
    CHECK (octet_length(data) = byte_size)
);
