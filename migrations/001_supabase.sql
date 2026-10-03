-- Additive, rerunnable migration. No existing Supabase tables are modified.
BEGIN;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '30s';
CREATE SCHEMA IF NOT EXISTS xyz_juicer;
REVOKE ALL ON SCHEMA xyz_juicer FROM PUBLIC;

CREATE TABLE IF NOT EXISTS xyz_juicer.sessions (
    id text PRIMARY KEY CHECK (length(id) = 64),
    data bytea NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry ON xyz_juicer.sessions (expires_at);

CREATE TABLE IF NOT EXISTS xyz_juicer.leases (
    session_id text PRIMARY KEY CHECK (length(session_id) = 64),
    owner text NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS leases_expiry ON xyz_juicer.leases (expires_at);

CREATE TABLE IF NOT EXISTS xyz_juicer.rate_limits (
    id text PRIMARY KEY CHECK (length(id) = 64),
    count integer NOT NULL CHECK (count > 0),
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_limits_expiry ON xyz_juicer.rate_limits (expires_at);

-- No browser policies: only the privileged backend connection may read data.
ALTER TABLE xyz_juicer.sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE xyz_juicer.leases ENABLE ROW LEVEL SECURITY;
ALTER TABLE xyz_juicer.rate_limits ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON ALL TABLES IN SCHEMA xyz_juicer FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON SCHEMA xyz_juicer FROM anon;
        REVOKE ALL ON ALL TABLES IN SCHEMA xyz_juicer FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON SCHEMA xyz_juicer FROM authenticated;
        REVOKE ALL ON ALL TABLES IN SCHEMA xyz_juicer FROM authenticated;
    END IF;
END $$;
COMMIT;
