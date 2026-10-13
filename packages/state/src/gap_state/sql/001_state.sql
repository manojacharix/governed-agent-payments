-- Working state: mutable, short-lived, no personal data beyond opaque refs (personal data lives in the profile store,
-- the record of truth in the ledger). One generic table; each row is one object of one kind.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'state_writer') THEN CREATE ROLE state_writer; END IF;
END $$;

CREATE TABLE state_items (
  kind        text        NOT NULL,             -- proposal | intent | recent | challenge | client | enrolment | caller | code | token | refresh | recovery | failure | suspension | notice
  key         text        NOT NULL,             -- the object's id (secrets are stored by their SHA-256, never in clear)
  scope       text        NOT NULL DEFAULT '',  -- the lock scope: the payer the object belongs to ('' for none)
  tag         text        NOT NULL DEFAULT '',  -- a secondary filter (e.g. the wheel)
  status      text        NOT NULL DEFAULT '',
  data        jsonb       NOT NULL,             -- the object, encoded losslessly by gap_core.codec
  version     bigint      NOT NULL DEFAULT 1,   -- optimistic check: every update names the version it read
  expires_at  timestamptz,                      -- when the object may be purged
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, key)
);
CREATE INDEX state_items_scope ON state_items (kind, scope);
CREATE INDEX state_items_tag ON state_items (kind, tag);
CREATE INDEX state_items_status ON state_items (kind, status) WHERE status <> '';
CREATE INDEX state_items_expiry ON state_items (expires_at) WHERE expires_at IS NOT NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON state_items TO state_writer;
