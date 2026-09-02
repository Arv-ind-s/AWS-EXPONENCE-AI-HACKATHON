# ADR-0002: Field encryption and key rotation

## Status

Accepted for T-017.

## Decision

Personal-class text columns use AES-256-GCM through the `EncryptedText`
SQLAlchemy type. The database value is a versioned envelope:

```text
cr1.<base64url key id>.<base64url 12-byte nonce>.<base64url ciphertext and tag>
```

The key identifier is metadata, not key material. It is included in the
associated authenticated data, so changing the identifier causes
authentication to fail. A new random nonce is used for every encryption;
equal plaintext values therefore do not produce equal ciphertext values.
Malformed envelopes, failed authentication, and unavailable key identifiers
are errors. They are never converted to `NULL`.

CIN lookup uses a separate 32-byte HMAC-SHA-256 key. Input is canonicalised by
removing Unicode whitespace and upper-casing before hashing. The complete
lowercase hexadecimal digest is stored. The fingerprint is deterministic for
lookup and uniqueness but does not reveal the CIN through a rainbow-table
guess without the separate HMAC key.

Keys are loaded only from the process environment or the operating system
keyring. The singular field key variable is suitable for a new deployment;
the JSON key-set variable permits old and new key IDs to coexist during a
rotation. The active key ID is non-secret environment metadata. The loader
rejects empty, malformed, incorrectly sized, or shared field/fingerprint key
material without logging its value.

## Rotation procedure

1. Generate a fresh 32-byte field-encryption key and place it in the approved
   environment or OS-keyring entry under a new key ID. Keep every old key
   needed by existing rows available until the run verifies complete.
2. Set the active key ID to the new ID and restart the application so startup
   validation proves the complete key set is available.
3. Run the database adapter's rotation job. It fetches rows in a stable ID
   order, decrypts each old-key value, and writes a newly encrypted value in
   one transaction per batch.
4. The job commits a batch before persisting its cursor. If the process stops
   after the commit but before the cursor write, that batch is replayed;
   values already on the active key are skipped, so replay is safe. If a
   transaction fails, the cursor does not advance.
5. Resume with the same job name and durable checkpoint store. Continue until
   the job reports an empty fetch and `complete=true`. Inspect the progress
   record and independently verify that no ciphertext carries an old key ID.
6. Retain old keys for the configured retention and backup period. Remove an
   old key only after the retention owner confirms that no live row, backup,
   or recovery image requires it.

The rotation seam accepts a database-owned checkpoint implementation instead
of creating an untracked table or writing a checkpoint file. The database
adapter is responsible for making that checkpoint durable and for making
`commit_batch` transactional.

## Consequences and limits

- Database readers without the field key see only authenticated envelopes.
- Database administrators can still see row counts, ciphertext lengths,
  key IDs, fingerprints, and other non-secret schema metadata.
- Encryption does not protect a running application process, an authorised
  caller, memory dumps, backups that lack database-level protection, or a
  compromised host holding the approved key.
- HMAC fingerprints support equality lookup only; they are not reversible
  decryption and cannot be used to recover the original CIN.
- Key rotation changes ciphertext and requires the old key until every value
  has been successfully re-encrypted.
