-- Profile store (19 §11, §12): personal data, encrypted per payer. Records are never edited;
-- only a data key's status, wrapped key and retention hold may change, which is how shredding works.

CREATE TABLE data_keys (
  dek_id       text        PRIMARY KEY,
  payer_ref    text        NOT NULL,
  wrapped      bytea,                       -- NULL once shredded
  kek_ref      text        NOT NULL,
  status       text        NOT NULL CHECK (status IN ('active', 'held', 'shredded')),
  hold_basis   text,
  hold_until   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  shredded_at  timestamptz,
  CHECK ((status = 'shredded') = (wrapped IS NULL))
);
CREATE UNIQUE INDEX data_keys_one_live_per_payer ON data_keys (payer_ref) WHERE status <> 'shredded';

CREATE TABLE profile_records (
  record_id    text        PRIMARY KEY,
  profile_ref  text        NOT NULL UNIQUE,
  payer_ref    text        NOT NULL,
  kind         text        NOT NULL,
  dek_id       text        NOT NULL REFERENCES data_keys (dek_id),
  nonce        bytea       NOT NULL,
  ciphertext   bytea       NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE recovery_secrets (           -- Argon2id hashes only (07 §3, product recovery questions)
  payer_ref    text        NOT NULL,
  slot         int         NOT NULL,
  argon2_hash  text        NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (payer_ref, slot)
);

CREATE FUNCTION profile_no_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'profile_records is insert-only (% rejected)', TG_OP USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER profile_records_insert_only BEFORE UPDATE OR DELETE ON profile_records
  FOR EACH ROW EXECUTE FUNCTION profile_no_change();

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'profile_writer') THEN
    CREATE ROLE profile_writer NOLOGIN;
  END IF;
END $$;
REVOKE ALL ON data_keys, profile_records, recovery_secrets FROM PUBLIC;
GRANT SELECT, INSERT ON data_keys, profile_records, recovery_secrets TO profile_writer;
GRANT UPDATE (status, wrapped, hold_basis, hold_until, shredded_at) ON data_keys TO profile_writer;
