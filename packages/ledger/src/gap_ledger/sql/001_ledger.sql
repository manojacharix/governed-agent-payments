-- Ledger (19 §12): one append-only table. Edits and deletes are rejected by trigger, and every insert
-- must extend its chain: seq = head + 1 and previous_hash = head's record_hash.

CREATE TABLE ledger_records (
  chain_id      text        NOT NULL,
  seq           bigint      NOT NULL CHECK (seq >= 0),
  record_type   text        NOT NULL,
  record_id     text        NOT NULL,
  body          jsonb       NOT NULL,   -- the record without its signature (includes the chain link)
  record_hash   text        NOT NULL CHECK (record_hash ~ '^sha256:[0-9a-f]{64}$'),
  previous_hash text        NOT NULL CHECK (previous_hash ~ '^sha256:[0-9a-f]{64}$'),
  signature     jsonb       NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (chain_id, seq),
  UNIQUE (record_hash)
);
CREATE INDEX ledger_records_type_id ON ledger_records (record_type, record_id);

CREATE FUNCTION ledger_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'ledger_records is append-only (% rejected)', TG_OP USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER ledger_no_update_delete BEFORE UPDATE OR DELETE ON ledger_records
  FOR EACH ROW EXECUTE FUNCTION ledger_append_only();
CREATE TRIGGER ledger_no_truncate BEFORE TRUNCATE ON ledger_records
  FOR EACH STATEMENT EXECUTE FUNCTION ledger_append_only();

CREATE FUNCTION ledger_extend_chain() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  head_seq  bigint;
  head_hash text;
BEGIN
  SELECT seq, record_hash INTO head_seq, head_hash
    FROM ledger_records WHERE chain_id = NEW.chain_id ORDER BY seq DESC LIMIT 1;
  IF NOT FOUND THEN
    IF NEW.seq <> 0 THEN
      RAISE EXCEPTION 'chain %: first record must have seq 0', NEW.chain_id USING ERRCODE = 'check_violation';
    END IF;
  ELSE
    IF NEW.seq <> head_seq + 1 THEN
      RAISE EXCEPTION 'chain %: seq % does not follow head %', NEW.chain_id, NEW.seq, head_seq USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.previous_hash <> head_hash THEN
      RAISE EXCEPTION 'chain %: previous_hash does not match the head', NEW.chain_id USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF NEW.body->'chain'->>'chain_id' IS DISTINCT FROM NEW.chain_id
     OR (NEW.body->'chain'->>'seq')::bigint IS DISTINCT FROM NEW.seq
     OR NEW.body->'chain'->>'previous_hash' IS DISTINCT FROM NEW.previous_hash THEN
    RAISE EXCEPTION 'chain %: columns disagree with the record''s chain link', NEW.chain_id USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER ledger_extend_chain BEFORE INSERT ON ledger_records
  FOR EACH ROW EXECUTE FUNCTION ledger_extend_chain();

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledger_writer') THEN
    CREATE ROLE ledger_writer NOLOGIN;
  END IF;
END $$;
REVOKE ALL ON ledger_records FROM PUBLIC;
GRANT SELECT, INSERT ON ledger_records TO ledger_writer;
