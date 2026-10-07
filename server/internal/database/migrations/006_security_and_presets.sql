ALTER TABLE users ADD COLUMN otp_secret bytea;
ALTER TABLE users ADD COLUMN otp_pending bytea;
ALTER TABLE users ADD COLUMN otp_pending_expires timestamptz;
ALTER TABLE users ADD COLUMN otp_counter bigint NOT NULL DEFAULT -1;
CREATE TABLE recovery_codes (
 user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 code_hash bytea NOT NULL,
 PRIMARY KEY(user_id,code_hash)
);
CREATE TABLE tag_presets (
 name text PRIMARY KEY CHECK (char_length(name) BETWEEN 1 AND 24),
 color text NOT NULL CHECK (color ~ '^#[0-9a-fA-F]{6}$'),
 version integer NOT NULL DEFAULT 1
);
