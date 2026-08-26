# Security Model

Companion to [security-controls.md](security-controls.md), which covers the controls on *what the
agent may do to the warehouse*. This document covers the controls introduced when the system
became multi-tenant: **who is asking, what they may see, and what happens to their secrets.**

The two documents answer different questions and the distinction is worth keeping. A perfect SQL
guard does not stop one customer reading another's reports, and perfect tenant isolation does not
stop a generated `DELETE`.

---

## 1. The threat this model is built for

Not an attacker in the abstract — three specific ones:

| Adversary | What they want | What stops them |
|---|---|---|
| **A customer** poking at ids | Another company's questions, reports, data sources | Every tenant-scoped query filters by `organization_id` in SQL, and a miss is a 404 |
| **A curious employee** of the customer | Data their role should not reach; the audit trail of their own actions | A role ladder checked per action; an append-only audit log with no edit path |
| **An operator** with database access | Customer warehouse credentials; the ability to act as a customer | Credentials encrypted with a key held outside the database; API keys and share links stored only as hashes |

The third is the one most systems get wrong, and it is the reason two different techniques are
used for two different jobs — see §4.

---

## 2. Identity

There is **no login in this system**, and that is a stated decision rather than an omission. A
session layer would mean password storage, reset flows and an account model nothing else here
needs. Instead:

- A caller presents `Authorization: Bearer <key>`.
- The key resolves to a **`Principal`**: an organisation, a user, and a role.
- Every tenant-scoped repository read takes the organisation *from that principal*.

### Two modes, one setting

| `REQUIRE_AUTHENTICATION` | Behaviour | Intended for |
|---|---|---|
| `false` (default) | A request with no key is the default organisation's owner, flagged `anonymous` | A local demo, the container as it ships, the evaluation suite |
| `true` | A valid key is mandatory; there is no anonymous path at all | Any deployment serving more than one company |

A **bad** key is `401` in both modes. Presenting a wrong key is not the same as presenting none,
and collapsing the two would silently downgrade a revoked key into a demo session with owner
rights.

### Keys

- 256 bits from `secrets.token_urlsafe`, prefixed `aak_`.
- Stored as **SHA-256 only**. The key is shown once, at creation, and is not recoverable
  afterwards — not by an admin, not by an operator with a database dump.
- No salt, deliberately: these are high-entropy random tokens, not passwords, so there is no
  dictionary to attack and a salted hash could not be looked up by value.
- Compared with `hmac.compare_digest`, so a timing signal cannot leak a valid prefix.
- **Removing a member revokes their keys in the same transaction.** Leaving a removed person's key
  live is the whole point of being able to remove them.

---

## 3. Tenant isolation

### Where it is enforced

**In the SQL, not in the route.** A route can be added next month by somebody who has not read
this document; a query that filters by `organization_id` cannot return another tenant's row no
matter who calls it. Two functions deliberately look across organisations — resolving an API key
and resolving a share token — and both say so in their docstring. They are the two places to read
carefully.

### The column is `NOT NULL`

`runs.organization_id` and `reports.organization_id` are `NOT NULL` **with a default**. A nullable
column would force every read to decide what an unowned row means, and the first careless decision
there is a boundary leak. Migration 005 backfilled the pre-Phase-3 data into a *default
organisation* rather than leaving it null or deleting it.

`Principal.in_organization(None)` returns **False**. The permissive reading of a missing owner is
exactly how this kind of bug happens, and the check exists even though the schema should make it
unreachable — "unreachable" is a claim about today's code.

### 404, not 403

For anything tenant-scoped, another organisation's resource answers **404**. A 403 confirms that
the resource exists, and a sequence of those confirmations is an enumeration of another company's
work. `403` is reserved for a caller who *is* in the right organisation but whose role is
insufficient — there, telling them to ask for access is useful and reveals nothing.

### What the tests assert

`tests/integration/test_tenancy.py` creates **two real organisations** and checks isolation from
both directions for runs, traces, reports, exports, chart images, dashboard counts, data sources,
alerts and the audit trail. Checking one direction only would leave a filter written the wrong way
round undetected.

That file has already earned its place: it caught a real hole where a saved report landed in the
default organisation instead of the caller's — invisible to the person who saved it, and visible
to a tenant with nothing to do with it.

---

## 4. Secrets: two techniques, two jobs

| Secret | Technique | Why |
|---|---|---|
| Data source `connection_config` | **Encrypted** (Fernet: AES-128-CBC + HMAC) | It must be recovered to open a connection |
| API keys, share tokens | **Hashed** (SHA-256) | Nothing needs to read them back — only to check a presented value |

Conflating these is the common mistake. Storing an API key recoverably would let an operator with
database access *act as* a customer, which is a different and worse risk than being able to read
their warehouse host.

### The key lives outside the database

`SECRETS_KEY` is an environment variable. A key stored beside the ciphertext it protects is
obfuscation, not encryption: one backup dump would contain both halves.

Its absence is a **hard failure at write time**, surfaced as `503` — the service is working and
the deployment is incomplete, and the message says which. Storing a configuration in the clear
"for now" is the one thing that endpoint must not do.

### Redaction is the other half

Encrypting the column is worthless if the API returns the plaintext. So:

- The `connection_config` column is **not in the select list** of any read the API uses. Excluded
  rather than stripped afterwards, because a column that never leaves the database cannot be
  forgotten by a response model written later.
- `redact()` is an **allowlist per source type** — host, port, database, user, sslmode for
  Postgres; filename, delimiter, encoding for CSV. A field added next year is hidden until
  somebody classifies it, which is the right default.
- A withheld key is **named, not starred out**. `{"_withheld": ["password"]}` tells the reader
  what is missing; `{"password": "••••••"}` would be a statement about its length.
- The `summary` column sits beside the ciphertext, so `carries_secret()` is asserted against it at
  write time. Unreachable through the allowlist, checked anyway: it is the one place a mistake
  would put a password in a column the API returns.
- The **audit trail records the redacted summary**, never the config. An audit entry holding a
  password would defeat the encryption on the row it describes.

Fernet is authenticated, so a tampered ciphertext or a rotated key **fails to decrypt** rather
than decrypting to something plausible.

---

## 5. Roles

A strict ladder, not a permission matrix: `viewer < analyst < admin < owner`. Four roles and a
dozen actions do not need a matrix, and a ladder has the property that matters — it cannot be
misconfigured into letting a viewer invite people.

| Action | Needs |
|---|---|
| read runs, reports, team, data source list, alerts | `viewer` |
| ask a question, save/share/delete a report, decide an approval, manage alerts | `analyst` |
| manage the team, manage data sources, read the audit trail, issue keys | `admin` |
| delete the organisation | `owner` |

Two structural refusals, both because the alternative is an organisation nobody can administer:

- The **last owner** cannot be removed or demoted. An organisation with no owner cannot appoint
  one.
- **Owner is not invitable.** Ownership is transferred by promoting an existing member, so an
  organisation cannot acquire a second owner through a typo in an invitation.

---

## 6. Sharing

A share is a **capability with a lifetime**, so it is a row that can expire and be revoked — not a
boolean on the report.

| Visibility | Who can read |
|---|---|
| `private` | only the member who saved it |
| `team` | anybody in the organisation |
| `public` | anybody holding a link |

- Tokens are **hashed**, like API keys. The listing shows a prefix, which identifies a link without
  revealing it.
- **Expiry is enforced in the SQL**, alongside revocation and the token match. Checking it in
  Python would leave the decision to whichever caller remembered to make it.
- A **`team` link still requires membership.** "Share with my team" must not quietly mean "share
  with the internet", so the route checks the reader's organisation even though the link is valid.
- **Unknown, expired and revoked are the same 404.** Distinguishing them would tell the holder of a
  dead link whether it ever existed.
- The shared view is **narrower than the owner's**: no organisation id, no run id, no saver.
  Somebody holding a link is not a member.
- Use is counted and timestamped, so an owner can see a link is live before deciding to revoke it.

---

## 7. The audit trail

`agent.audit_log` records who did what: the actor's id *and* their label, the action, the target,
a JSON detail, and a timestamp.

- **Append-only by intent.** The repository exposes `audit` and `audit_entries` and nothing else —
  no update, no delete, no purge. A trail somebody can edit is not one, and a test asserts that no
  such function exists rather than merely that an `UPDATE` fails.
- `actor_label` is stored **alongside** `actor_user_id`, so an entry stays readable after the user
  row is gone.
- An **unauthenticated action is labelled as such**, so a demo session cannot be mistaken for a
  named person.
- An audit write **never fails the action it describes**: a failed insert is logged at error level,
  and the invitation still happened. The alternative turns a working feature into a 500 whenever
  the log has a problem.
- Reading the trail is `admin` and above. It names who did what, which is not a viewer's business.

Recorded actions: organisation creation, invitations, removals, role changes, key issue and
revoke, data source create and delete, report save, visibility change, share create and revoke,
alert create, status change, delete, and every alert that fired.

---

## 8. Alerts, and why they cannot invent a metric

An alert watches an **approved metric**, checked against the registry at creation time. It runs
unattended, so a definition somebody invented once would keep firing about a number nobody agreed
on.

An evaluation goes through `metric_query` like any other read: the registry renders the statement,
`sql_guard` validates it, it runs as `analyst_ro`, and it lands in `sql_audit`. Each evaluation
**creates a real run** owned by the alert's organisation — the first version used a throwaway id
until the foreign key on `tool_calls` refused it, correctly: a tool call with no run is a query
nobody can trace back to a reason.

Both outcomes are recorded, fired or not. Keeping only the breaches would leave no way to tell a
quiet alert from one that stopped running, and "we were never alerted" is the sentence that
follows the second.

---

## 9. What this model does not do

Stated plainly, because a security document that lists only its strengths is marketing.

- **No login, no sessions, no password storage.** Identity is an API key. The name recorded against
  an approval decision is typed by the person making it and is *unverified* — honest about it, but
  unverified.
- **No row-level security in the warehouse.** `analyst_ro` is one role for every tenant. Multi-
  tenant *analytical* data would need RLS or a schema per tenant; today the warehouse is shared
  demonstration data, and the isolation is over the agent's own tables.
- **Data sources are stored, not yet used.** A registered Postgres or CSV source is encrypted,
  listed and redacted correctly; the agent still queries the seeded `analytics` schema. Connecting
  the agent to a customer's own warehouse needs per-source guard catalogues and per-source
  read-only roles, and is not built.
- **No key rotation.** Rotating `SECRETS_KEY` would require re-encrypting every stored config;
  there is no migration path for it.
- **No rate limiting per organisation.** The existing limit is per endpoint, not per tenant, so one
  customer can exhaust shared model budget.
- **Share links are bearer capabilities.** Anybody with the URL can read a public report until it
  expires or is revoked. That is what a share link *is*, but it means the link is as sensitive as
  the report.
- **The audit trail is not tamper-evident.** It is append-only through the application, which stops
  the application editing it — not an operator with `UPDATE` on the table. Hash chaining or
  write-once storage would be the next step.

---

## 10. Deploying this safely

The short version, expanded in [deployment.md](deployment.md):

1. Set `REQUIRE_AUTHENTICATION=true`. Without it, anybody who can reach the port is an owner.
2. Set `SECRETS_KEY` to a fresh Fernet key, held in a secret manager and **not** in the same
   backup as the database.
3. Create a real organisation, issue keys per person, and delete the default organisation's key if
   one was ever issued.
4. Put TLS in front of the service. Bearer tokens over plain HTTP are shared secrets in transit.
5. Restrict `/v1/shared/{token}` at the edge if public sharing is not wanted — it is the only
   unauthenticated read path, by design.
