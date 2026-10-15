"""WebAuthn verification now lives in gap_core.webauthn, so the open evidence verifier checks payer proofs with the
same code the host does. Re-exported here for existing imports."""

from gap_core.webauthn import FLAG_UP, FLAG_UV, SoftwareAuthenticator, WebAuthnError, verify_assertion  # noqa: F401
