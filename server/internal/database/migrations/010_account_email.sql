ALTER TABLE users ADD COLUMN email_verified_at timestamptz;
ALTER TABLE users ADD COLUMN email_verification_required boolean NOT NULL DEFAULT false;
CREATE TABLE email_tokens (
    token_hash bytea PRIMARY KEY CHECK(octet_length(token_hash)=32),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose text NOT NULL CHECK(purpose IN ('verify','reset')),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX email_tokens_owner ON email_tokens(user_id,purpose,created_at DESC);
CREATE TABLE email_outbox (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose text NOT NULL CHECK(purpose IN ('verify','reset')),
    payload bytea,
    state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sending','accepted','failed','cancelled')),
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    provider_id text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX email_outbox_pending ON email_outbox(available_at) WHERE state IN ('pending','sending');
CREATE INDEX email_outbox_owner ON email_outbox(user_id,created_at DESC);
