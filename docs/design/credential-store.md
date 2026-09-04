# Credential store — design note (v1.1, deferred)

**Status:** deferred out of v1 · **Supersedes:** SPEC.md §6.7 as of Draft v1.2 · **Date:** 2026-09-04

v1 resolves cloud credentials from the host's own chain only (SPEC.md §6.1). This
note holds the design for storing them, which lands in v1.1, together with the
seven problems that must be solved before any of it is written.

## Why it was deferred

Ambient credentials cover the deployed case and none of the common one: an analyst
on a laptop, auditing a bucket in an account they do not administer, handed a
read-only key by the team that does. That user is real, and this feature is for
them.

It was still cut from v1, for three reasons:

1. **The v1 fallback is close enough.** For AWS, a handed-over key goes in
   `~/.aws/credentials` under a named profile — which SPEC.md §6.6 already accepts
   as `auth: {profile: ...}`. Same key, same disk, same `0600`. The sealed store's
   genuine advantage over that is protection of a *stolen backup or synced folder*,
   which is worth building and is not what blocks the first user.
2. **Most of the unresolved work is in the security claims, not the plumbing.**
   Five of the seven problems below are about whether a stated protection actually
   holds. This is the one part of the system whose failure mode is not a wrong
   number.
3. **It comes back better against a working tool.** Problem 1 in particular wants
   the destination bound into the envelope from the first version, rather than
   added to an AAD that already shipped.

Azure is the one real gap: there is no clean ambient-file equivalent to a named
AWS profile for a container SAS. v1 accepts `AZURE_STORAGE_SAS_TOKEN` from the
environment and says plainly that persistent storage arrives in v1.1.

## Problems to solve first

These came out of an adversarial review of the v1.2 draft. **None of them is a
detail to settle during implementation** — each one changes the shape of the
envelope, the config, or a claim made to the user.

### 1. The config file steers the key and nothing authenticates it

The AAD below binds everything about the *secret* and nothing about its
*destination*. The destination — `uri`, `account`, `region`, `role_arn`, and for
S3-compatible stores an explicit `endpoint_url` — lives in plaintext
`connections.yaml`, so an attacker who can edit that file redirects a credential
without breaking any authentication tag.

For `aws_access_key` this sends a SigV4-signed request (and the access key id) to
an attacker's host. For **`azure_sas` — the preferred Azure kind — the credential
*is* a URL query string**, so redirecting the endpoint hands the token over in
plaintext.

This invalidates the claim "a connection cannot be tricked into sending one
account's key to another account's endpoint."

**Required:** destination constraints (account/container, bucket, permitted
endpoint host) live *inside the sealed record* and go into the AAD. A config that
disagrees with the sealed record fails authentication rather than being obeyed.
Query-string credentials get an additional hard rule: never transmitted to a host
not pinned in the record.

### 2. Rollback detection does not work as specified

An HMAC over the record list and a monotonic counter, with the counter stored in
the file, cannot detect a restored older copy of that file: the old copy carries
its own consistent index, its own valid HMAC, and its own lower counter.

**Required:** the high-water mark lives somewhere the attacker is not restoring.
The keyring backend can hold it next to the root key. The passphrase backend has
nowhere — so rollback is detectable within a session and not across a restart, and
that limit is stated rather than tested around.

### 3. The keyring claim overstates what the OS provides

"The OS owns the key and its unlock policy" is not what happens in practice. macOS
Keychain ACLs trust an *application*, which via `keyring` is the Python
interpreter — so any script the user runs under the same interpreter reads the root
key back without a prompt. On Linux Secret Service, any process in the session can
read it. Touch ID gates the login keychain unlock, not per-item access for an
already-trusted binary.

**Required:** the wording becomes "the OS protects the key at rest under the
user's account; it does not protect it from other processes running as that user."
The stolen-file threat model is unaffected and remains the claim.

### 4. Idle re-lock is a one-way door for the server

The passphrase root key is derived at `serve` startup, never from the browser, and
the store re-seals when idle. After the first re-lock there is no path back: you
cannot prompt on a daemonized server's terminal, and the browser is forbidden.
Every credentialed run fails until the process restarts.

**Required:** pick one — drop idle re-lock for the server and keep it for the CLI
(preferred; it defends against a threat the model already concedes), add a
terminal-side unlock channel, or accept restart-to-unlock and document it.

### 5. Argon2id parameters fight the environment that needs them

"Memory in the hundreds of MiB" is libsodium's MODERATE (256 MiB) or SENSITIVE
(1 GiB). The passphrase backend exists for **container installs**, which is exactly
where that allocation meets a memory limit. Parameters travel with the ciphertext,
so a store sealed on a laptop is openable on a small container only if the
container can afford the laptop's memlimit — otherwise it is OOM-killed rather than
failing cleanly.

**Required:** a stated parameter floor, a "this store needs N MiB to open" error
instead of a kill, and `rekey` documented as the downgrade path for constrained
hosts.

Related: `mlock` protects the 32-byte root key, but Argon2id's own
several-hundred-MiB working buffer holds key material and is not locked. The
zeroization caveat below must be extended to say so.

### 6. Credential chains and the `test` verb

- `aws_role` is described as assumable "via ambient or another stored credential" —
  a credential pointing at a credential, with no depth limit and no cycle
  detection. **Required:** cap the chain at one hop, or resolve roles against
  ambient only.
- `credentials test <ref>` uses a secret against a target the ref does not name.
  **Required:** it exercises only connections that already reference the ref, and
  only within the configured source scope (SPEC.md §6.1). Otherwise it is a "make
  the server use this key against a host I name" primitive with a friendly name.

### 7. Metadata sits outside the authenticated context

`last_used` and `expiry` are not in the bound context, so an expired SAS can be
made to look current and use history can be edited. **Required:** the index HMAC
covers all metadata, not only the record list.

---

## The design, as it stood

Everything below is the v1.2 §6.7 text, carried forward unchanged except where the
problems above already contradict it. It is a starting point, not an approved
design.

### Credential kinds, ordered by preference

Short-lived and narrowly scoped beats long-lived and broad, and the app is
opinionated about it — the UI presents them in this order and marks the last of
each group as discouraged.

| Connector | Kind | Notes |
|---|---|---|
| **s3** | `aws_role` | Role ARN (+ optional external id), assumed via ambient. Session credentials live in memory only and refresh on expiry. **Preferred.** |
| | `aws_access_key` | Access key id + secret (+ optional session token). Long-lived; stored with a created-at date and flagged once it ages past a configurable threshold. |
| **az** | `azure_sas` | Container-scoped SAS token — read-only and expiring by construction. **Preferred**, and the app warns as the expiry approaches rather than failing a run at 3am. |
| | `azure_client_secret` | Service principal: tenant id, client id, secret. Scoped by RBAC role assignment. |
| | `azure_storage_key` | Account key. Full control of the whole account; accepted, discouraged in the UI, and never the default. |

### One encrypted store, two ways to get the key

Rather than two storage formats, there is **one sealed store** and two sources for
its master key. The store is always encrypted; the backends differ only in who
holds the 32-byte root key.

1. **OS keyring (default).** A random 32-byte root key is generated at store
   creation and kept in macOS Keychain, Windows Credential Manager, or Linux Secret
   Service, namespaced by workspace path. The OS protects it at rest under the
   user's account — subject to problem 3.
2. **Passphrase (headless and container installs).** The root key is derived from a
   passphrase, supplied by environment variable or an interactive prompt **at
   `serve` startup**, never from the browser. The asymmetry is deliberate: the
   person at the terminal unlocks the store; a browser session can add a credential
   to an unlocked store but can never unlock one. Subject to problem 4.

Because both paths converge on the same root key and the same file, switching
between them is a **rekey**, not an export-and-reimport — the plaintext secrets are
never handed back out to migrate them.

### Algorithms

Named, not implemented. Everything comes from libsodium via PyNaCl; no primitive is
written in this project, and no algorithm is chosen at runtime by anything but the
envelope header.

| Purpose | Choice | Why this one |
|---|---|---|
| Passphrase → root key | **Argon2id** (RFC 9106), 16-byte random salt, moderate-or-higher parameters (subject to problem 5) | Memory-hard, so a stolen file is expensive to attack offline with GPUs. Parameters are stored in the header, not assumed. |
| Root key → per-record key | **HKDF-SHA-256**, `info = "cred/v1/" ‖ secret_ref` | One key per record, so a compromise is scoped and a record cannot be moved between refs. |
| Record encryption | **XChaCha20-Poly1305-IETF**, 24-byte random nonce per write | AEAD with a nonce large enough that random generation is safe without a counter — the misuse that breaks AES-GCM deployments does not arise. |
| Index integrity | **HMAC-SHA-256** over the record list, all metadata, and a monotonic version counter | Detects deletion of records, which per-record AEAD cannot see. Rollback needs problem 2 solved. |
| Display fingerprint | first 8 hex of **HMAC-SHA-256**(display key, secret) | Lets a user confirm *which* secret is stored without revealing any of it. Note it also reveals key reuse across refs, which is acceptable and should be stated. |
| Access token check | constant-time comparison; persisted form is an **Argon2id** hash | A timing-safe compare, and a token file that is not a plaintext token. |

**Per-record sealing, with context bound in.** Each credential is sealed
individually under its own derived key, with associated data covering
`envelope_version ‖ secret_ref ‖ kind ‖ created_at ‖ store_id` **plus the
destination constraints of problem 1**. Consequences that whole-file encryption
would not give:

- **Records cannot be relabelled or swapped.** Moving the ciphertext for
  `prod-readonly` onto the ref `staging-readonly`, or editing a stored `kind` to
  make an account key look like a scoped SAS token, fails authentication instead of
  succeeding quietly.
- **Rotate and delete touch one record.** No rewriting the whole file, no window
  where every secret is in memory at once, and a corrupted record loses one
  credential rather than all of them.

**Versioned envelope, no algorithm guessing.** Every record and the index carry an
explicit version naming the KDF, its parameters, the AEAD, and the salt/nonce.
Readers refuse an unrecognized version outright rather than inferring one, and
`credentials rekey` re-seals the store under current parameters — so raising
Argon2id cost later does not strand an existing store, and a downgrade cannot be
forced by editing a header.

**Key and plaintext handling.** The root key is held in locked memory (`mlock`) and
zeroed on lock, rekey, and exit; a per-record key exists only for the duration of
one seal or open. Secret plaintext is held in mutable buffers that are zeroed after
use. On disk the store is mode `0600` inside a `0700` directory, written by sealing
into a temporary file and atomically renaming over the old one with an fsync — so
an interrupted write cannot truncate the store, and no plaintext ever reaches a
temporary file.

> **Stated honestly:** Python cannot guarantee zeroization — an immutable `str`
> created anywhere in the path, a cloud SDK copying the value into its own signer,
> or a garbage-collected buffer may leave a copy behind. Argon2id's own working
> memory is not locked either (problem 5). The buffers we control are wiped, the
> boundary where they stop being ours is the SDK call, and no claim beyond that is
> made.

**What this does not protect against**, stated so nobody reads "encrypted" as
"safe": a compromised host, a process already running with the store unlocked, a
hostile dependency inside the process, a core dump, or a user who pastes a key into
the wrong field. Encryption at rest protects a *stolen file* — a backup, a synced
folder, a laptop — and that is precisely the threat it is here for.

**Crypto correctness is tested as its own thing**, not implied by the feature
working: known-answer vectors for the KDF and AEAD; a tamper suite flipping bits in
ciphertext, nonce, associated data, and header and asserting each fails closed with
no plaintext returned; a wrong passphrase asserting failure rather than garbage; a
cross-ref swap asserting rejection; a redirected destination asserting rejection
(problem 1); a rolled-back index asserting detection within the limits of problem
2; and a rekey round-trip asserting every ref still opens.

### Write-only, from every direction

The store accepts secrets and does not return them. There is no API endpoint, CLI
command, template, or log line that emits a stored secret value — only metadata:
kind, display fingerprint, created, last used, expiry, and any non-secret
identifier the kind carries (an AWS access key *id* is not a secret; its secret
half never appears). Retrieval happens in-process, when a connector asks for it,
and nowhere else. A local server that can be asked to read back its own secrets
turns any stray browser tab or local process into an exfiltration path.

### The bind interlock

SPEC.md §12 says the server ships without authentication because a single-tenant
local server has nothing to authenticate. A credential store changes that fact, so
the rule changes with it:

> If the credential store is non-empty **and** the bind address is not loopback,
> the server **refuses to serve** without a configured access token.

**Checked at two moments, not one.** A startup-only check is trivially bypassed:
start with an empty store on `0.0.0.0`, then add a credential through the browser,
and the interlock never fires again — leaving exactly the configuration it exists
to forbid.

1. **At `serve` startup** — a non-empty store plus a non-loopback bind and no token
   file: refuse to start, naming both conditions.
2. **At every write to the store** — `PUT /api/credentials/{ref}` on a tokenless
   non-loopback bind is rejected with an error explaining that the server must be
   restarted with `--auth-token-file`, or bound to loopback. The first credential
   cannot be added through the hole that adding it would open.

Non-emptiness is readable while the store is locked, since metadata is not sealed —
so the interlock never requires an unlock to enforce itself.

The CLI path (`credentials add`) is unaffected: it is a terminal operation on a
store the server may not even be running against.

**Loopback is defined, not assumed:** `127.0.0.0/8`, `::1`, and any hostname that
resolves entirely within them. Anything else — a LAN address, `0.0.0.0`, `::` — is
non-loopback, and `0.0.0.0` is treated as non-loopback even though it *includes*
loopback, because it also includes everything else.

Refuses, not warns — a warning printed at startup is not a control. Loopback binds
are exempt from the token requirement, and the honest reason is narrower than it
sounds: a loopback bind limits exposure to processes on the host, which is not the
same as limiting it to the invoking user. This is the smallest honest amount of
authentication: a single shared token compared in constant time, held in an
HttpOnly, SameSite cookie. It is not an identity system — there are still no
accounts.

### Secrets never leave the process

- Connectors receive a credential object whose string and repr forms are masked, so
  a secret cannot reach a log or a traceback by accident.
- Nothing is written to `run.json`, `manifest.json`, `log.jsonl`, an event stream,
  or a report — the run record names the `secret_ref`, never the value.
- Credentials are **not part of the workspace**: copying a run store to a
  colleague, or committing one, never carries secrets with it. The keyring backend
  is outside it entirely, and the file backend is excluded from every export path.
- Secrets are read from stdin or an interactive prompt, never from command-line
  arguments, which are readable by every process on the machine.

### Lifecycle and accountability

- **Rotate in place.** A new value under the same `secret_ref` — connections keep
  working, no config edit, and the previous value is overwritten rather than
  versioned.
- **Delete removes the record and names the connections that will break.** It does
  not promise erasure of the underlying blocks: an atomic rename writes a new file,
  and on SSDs and copy-on-write filesystems the old one may persist.
- **Expiry is tracked** where the kind has one (SAS tokens, assumed-role sessions),
  surfaced on the Connections page, and warned about before it bites.
- **Use is audited.** Every resolution emits a `credential_used` event into the run
  record naming the ref, the connection, and the operation — so "what did this
  server do with my key" has an answer that does not require trusting anyone's
  memory.
- **Idle re-lock**, subject to problem 4. A **queued** run that needs a locked
  credential stays queued and reports `credential_locked`; a run already
  **executing** when the store re-locks is failed, not paused.

### Config shape — the file references, the store holds

```yaml
connections:
  - id: prod-bedrock-s3
    connector: s3
    uri: s3://acme-llm-logs/bedrock/
    region: us-east-1
    auth:
      secret_ref: acme-audit-readonly      # resolved from the credential store
      role_arn: arn:aws:iam::123456789012:role/llm-audit-readonly

  - id: foundry-diagnostics
    connector: az
    uri: az://insights-logs-requestresponse/
    account: acmellmlogs
    auth:
      secret_ref: acme-foundry-sas         # kind: azure_sas, expires 2026-12-31
```

A config file remains safe to commit: it contains references and no secret
material. Problem 1 additionally requires that the destination fields above be
*checked against* the sealed record rather than trusted from this file.
