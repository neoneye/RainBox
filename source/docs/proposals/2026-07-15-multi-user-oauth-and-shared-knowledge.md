# Proposal: multiple users — OAuth login, connector guests, shared knowledge

**Status:** Draft / proposal (rev. 1)
**Date:** 2026-07-15
**Scope:** The long-term identity design: named users logging in via OAuth on
the web UI, auto-provisioned guest users arriving through connectors
(Telegram/Discord), an admin with elevated permissions, and knowledge
(Q&A overlay, memory claims, profiles, settings) that is partly per-user and
partly shared. This supersedes the single-operator assumption stated in
`2026-06-07-user-configuration-in-postgres.md` ("settings are global … if the
app ever becomes multi-tenant, user context/RBAC will be a larger design").
This is that larger design.

## Motivation

Today the app is single-operator by construction:

- No authentication at all — every route on `127.0.0.1:5000` is open, and
  `chat_user` holds exactly one human row by design (`docs/data-model.md`).
- The Telegram bridge (`telegram_service/bridge.py`) funnels **every** allowed
  Telegram sender through that one human uuid — two family members chatting
  from two phones are indistinguishable, and both speak with the operator's
  full authority.
- Knowledge is one pool: one `question_answer.jsonl` + one `customize.dir`
  overlay, one set of memory claims, one `profile.current`. The assistant
  cannot answer "who am I talking to?" differently per sender, and it cannot
  keep one person's private facts out of another person's answers.

The target shape is a small trusted group — a family, or a company team:
a handful of named people, each with their own private knowledge and
customization, plus pools of shared knowledge (family memories, company
facts), plus drive-by guests on chat connectors who get a useful but
unprivileged assistant.

## Goals / non-goals

**Goals**

- Named users authenticate on the web via OAuth (Google first, generic OIDC).
- Chat-connector senders (Telegram, Discord) resolve to users; unknown senders
  become **guests** automatically — no signup flow on the phone.
- One person is the **admin** (the operator today) with elevated permissions;
  everyone else is a **member** or a **guest**.
- Knowledge carries an **audience**: per-user private, shared spaces, and
  global. The Q&A overlay, memory claims, profiles, and relevant settings all
  gain that dimension. Retrieval filters by the requesting user's audiences.
- Full fidelity storage, layered visibility: multi-user privacy is achieved by
  audience filtering at read time, never by storing less (per the operator's
  standing rule — privacy is visibility layers, not data reduction).
- Fail-closed everywhere a decision is ambiguous.

**Non-goals**

- Internet-scale multi-tenancy (orgs, billing, quotas, thousands of users).
  The design targets ~2–20 accounts that mostly trust each other.
- Making the Flask app itself internet-hardened. Exposure beyond localhost
  still goes behind a reverse proxy with TLS; this proposal adds *identity*,
  not perimeter hardening (rate limiting, WAF, CSP audits are separate work).
- Row-level ACLs finer than spaces (per-claim share-with-one-person grants).
  Spaces cover the family/company cases; finer grants are an easy later
  migration because the audience column already exists.
- Password auth. OAuth/OIDC only on the web; connector identity on bridges.

## Design overview

Three new concepts, each one table plus glue:

1. **Account** — who a person is inside rainbox (role, status, display name).
2. **Identity** — how the outside world proves which account is talking
   (a Google subject, a Telegram user id, a Discord user id). Many identities
   can point at one account: the same person on web + phone is one account.
3. **Space** — an audience for knowledge (personal, shared, global). Knowledge
   rows point at a space; users read what their space memberships allow.

Everything else is threading: the web session or bridge resolves an identity
to an account, the account rides the request/turn context, and every knowledge
read filters by that account's spaces while every knowledge write stamps one.

```
Google OAuth ──┐
Telegram from_id ──┤→ account_identity → account ──┬→ role (admin/member/guest)
Discord author.id ─┘                               ├→ chat_user (chat presence)
                                                   └→ space memberships
                                                        ├─ personal space
                                                        ├─ shared spaces
                                                        └─ global
```

## Identity model

### `account`

| column        | type      | notes                                            |
|---------------|-----------|--------------------------------------------------|
| id / uuid     | pk / uuid | usual pattern                                    |
| display_name  | text      | what the assistant and UI call them              |
| role          | text      | `admin` \| `member` \| `guest` (CHECK)           |
| status        | text      | `active` \| `pending` \| `disabled` (CHECK)      |
| profile_uuid  | uuid null | this account's own /profile row (the per-user successor of the global `profile.current` setting) |
| created_at / updated_at | | |

- Exactly the people. Agents are **not** accounts — they stay `chat_user`
  rows of `user_type='agent'` as today.
- `pending` is the fail-closed state for a first OAuth login that no rule
  auto-approves (see Login rules): the person authenticated but can do nothing
  until the admin approves.

### `account_identity`

| column       | type      | notes                                              |
|--------------|-----------|----------------------------------------------------|
| id / uuid    | pk / uuid |                                                    |
| account_uuid | uuid FK   | → account                                          |
| provider     | text      | `google` \| `oidc` \| `telegram` \| `discord` (CHECK) |
| external_id  | text      | OIDC `sub`, Telegram user id, Discord user id      |
| claims       | jsonb     | provider snapshot: email, username, avatar — display/debug only, never authorization |
| created_at / last_seen_at | | |

- Unique on `(provider, external_id)`. Authorization decisions key on this
  pair only — never on mutable claims like email or username.
- Linking a second identity to an existing account (web user also on
  Telegram) is an admin action in the UI, or self-service via a short-lived
  link code the assistant can hand out in chat ("say `link 4F7Q` on the web").

### `chat_user` linkage

Add nullable `account_uuid` to `chat_user`. Each account gets its own human
`chat_user` row (created on account creation), so:

- `chat_message.sender_uuid` keeps working unchanged — messages from
  different people are different senders, which the chat UI and the
  assistant's `conversation_history` already render per-sender.
- The existing single human row is backfilled as the admin's chat presence.
  No message rewriting; history stays intact (non-destructive migration).

## Authentication

### Web: OAuth via Authlib (OIDC)

- Google first (covers the operator), plus a generic OIDC provider entry so a
  company IdP works later. Authlib's Flask integration; standard
  authorization-code flow; we consume `sub`, `email`, `name`.
- Server-side session: Flask session cookie holds only the account uuid;
  `SECRET_KEY` becomes a required real secret (env-only, same rule as other
  secrets in `db/settings.py`) — the app refuses to start multi-user mode with
  the `rainbox-dev` default.
- Client config (`client_id`, `client_secret`, redirect URL) is env-only
  (secrets never land in `app_setting`, per the existing threat model in
  `docs/backup.md`).

**Login rules** (fail-closed, in order):

1. Identity already linked → that account.
2. First login ever (no accounts exist) → becomes the **admin** account.
   This is the bootstrap path for a fresh install; existing installs instead
   get the admin account from the migration (below), and the operator links
   their Google identity from /settings while still on localhost.
3. Email domain on the allowlist (`auth.member_domains`, admin-set) →
   auto-provision an active **member** (the company case).
4. Otherwise → auto-provision a **pending** account: authenticated, but every
   page says "waiting for approval" until the admin flips it (the family
   case: approve your relatives one by one).

### Connectors: verified platform identity, no OAuth

Telegram and Discord senders are already authenticated *by the platform*; the
bridge trusts the bot API's `from.id` / `author.id` the same way it does
today, then resolves it:

1. `(provider, external_id)` linked → that account. The admin texting from
   their phone is the admin, with admin capabilities.
2. Unknown sender → auto-provision a **guest** account (+ identity +
   chat_user + personal space), display name from the platform profile.
   A guest exists only inside chat: no web login, no /settings, no memory
   review — but their conversations and remembered facts persist in their
   personal space, so a later promotion to member keeps their history.
3. A per-connector policy setting gates step 2:
   `connector.guest_policy` = `open` (anyone may become a guest) \|
   `allowlist` (today's behavior, ids listed) \| `closed` (linked accounts
   only). Default `allowlist` — matching current behavior is the safe default.

The Discord bridge is a sibling of `telegram_service/` with the same
resolve-then-post shape; the identity resolution function is shared code, so
a third connector later (Signal, Slack) is one adapter.

## Authorization

Three roles, deliberately coarse (spaces do the fine-grained work):

| capability                                | admin | member | guest |
|-------------------------------------------|:-----:|:------:|:-----:|
| chat with the assistant                    | ✓ | ✓ | ✓ |
| own personal space (memories, Q&A, profile)| ✓ | ✓ | ✓ |
| read shared spaces they belong to          | ✓ | ✓ | ✓ |
| write to shared spaces                     | ✓ | per-membership | — |
| web login                                  | ✓ | ✓ | — |
| /settings, backups, cron, capability toggles | ✓ | — | — |
| memory review queues, Flask-Admin          | ✓ | — | — |
| approve accounts, manage spaces & links    | ✓ | — | — |
| assistant write-actions (kanban, files, …) | ✓ | configurable subset | read-only actions |

Enforcement is two-layered:

- **Web routes:** a `require_role(...)` decorator; every existing page gets a
  tier (public: none; member: chat, own profile; admin: everything else).
  Unauthenticated hits redirect to login. A `single_user_mode` setting
  (default on for existing installs) keeps today's no-login localhost
  behavior until the operator flips it — the migration is opt-in.
- **Assistant capabilities:** the per-turn capability set
  (`enabled_capabilities()`), which already exists for the operator's
  disable-toggles, additionally intersects with the *sender's role policy*.
  A guest asking "delete the kanban board" simply has no such action in the
  catalog — the same mechanism that hides operator-disabled capabilities
  today, so prompt and dispatch stay consistent for free.

## Spaces: the audience model for knowledge

### `space` and `space_member`

| space column | notes                                        |
|--------------|----------------------------------------------|
| uuid, name   | e.g. "Personal — Ada", "Family", "Company"   |
| kind         | `personal` \| `shared` (CHECK)               |
| owner_account_uuid | for personal spaces, the person        |

| space_member column | notes                                  |
|---------------------|----------------------------------------|
| space_uuid, account_uuid | pk pair                           |
| role                | `reader` \| `contributor` (CHECK)      |

- Every account gets a personal space at creation (owner = them).
- The admin creates shared spaces and manages membership from the web UI
  ("Family" with everyone as contributor; "Company" with staff as readers and
  leads as contributors).
- **Global** is not a row: a NULL `space_uuid` on a knowledge row means
  "visible to every account", which is also exactly what all pre-migration
  rows mean — the backfill is free and honest (everything today *was* global
  to the one operator).

A request's **read set** = global (NULL) + own personal space + every space
membership. A **write** stamps exactly one space: personal by default; a
shared space when the actor says so ("remember for the family: …") or when
the room is bound to a space (below).

### Memory claims

- Add nullable `space_uuid` to `memory_claim`. Existing `scope`
  (global/agent/room/project) and `sensitivity` (public/private/secret) stay
  orthogonal and unchanged: scope answers *where* a claim applies,
  sensitivity answers *how carefully* it's surfaced, space answers *who* may
  see it.
- `hard_filtered_claims()` gains an `account` parameter and adds
  `space_uuid IS NULL OR space_uuid IN (read set)` to the existing hard
  filter — one place, already the shared choke point for both hybrid recall
  and the profile digest, so every retrieval path inherits the rule at once.
- Claim writes through the assistant (`memory_remember`) stamp the sender's
  personal space unless the turn context says a shared space.

### Q&A knowledge (`question_answer.jsonl` + customize.dir)

Today: base file + one overlay directory, merged by entry id, embedded into
one LlamaIndex pgvector table, with `shield` gates. Multi-user layout:

```
<customize.dir>/
  question_answer.jsonl              # global overlay (as today)
  spaces/<space-slug>/question_answer.jsonl   # one overlay per space
```

- Merge precedence per requesting user: base → global overlay → their shared
  spaces' overlays → their personal overlay (most specific wins by id).
- Embedded rows carry `space` in their vector-store metadata; Q&A retrieval
  filters to the requester's read set *before* similarity ranking (filter
  before rank, the same contract the memory layer already follows).
- Shields stay as-is but the *unlocked set* moves to per-account settings —
  what the admin has unlocked for themselves must not unlock for a guest.
- Slug→space binding lives in the space row (a `slug` column) so renaming a
  directory can't silently re-audience knowledge.

### Profiles and the identity block

- `profile.current` (global, from the 2026-07-14/15 work) is superseded by
  `account.profile_uuid` — each account points at its own /profile row, and
  the `<operator_identity>` block (already JSON) renders *the sender's*
  profile. The single-user setting stays honored in `single_user_mode`.
- The prompt gains the sender's role so the assistant can calibrate tone and
  refuse out-of-scope requests early:
  `<requester format="json">{"name": "Ada", "role": "guest"}</requester>`.
- Profile rows gain nullable `owner_account_uuid` + `space_uuid`: your own
  profile is yours; "people we both know" profiles live in a shared space;
  today's rows backfill as global, matching current behavior.

### Settings

Keep `app_setting` global — it is infrastructure (backup, cron, providers)
and stays admin-only. Add a parallel `account_setting` table
(`account_uuid, key, value`) with its own small registry for the keys that
are genuinely per-person: `chat.default_model` (per-account override, global
fallback) and `qa.unlocked_shields` (moves here). The current profile is NOT
an account_setting — it is the `account.profile_uuid` column, because it is
identity, not preference.
Same registry pattern (type/default/validate), same precedence idea:
account value → global default. This is the "easy migration" the 2026-06-07
proposal predicted, done as a separate table instead of a scope column so the
global registry's reconcile-on-startup logic stays untouched.

## Rooms and turn context

- `chatroom_member` becomes enforced: the web chat lists only rooms you're a
  member of; bridge rooms auto-add the resolved account's chat_user. Agents
  remain members as today.
- A room may optionally bind to a space (`chatroom.space_uuid`): a "Family"
  room where remembered facts default to the family space. Unbound rooms
  default writes to the sender's personal space.
- The assistant's turn context becomes
  `(room, sender account, capability set, read set)` — today it is
  effectively `(room)`. The sender is the author of the triggering message,
  resolved once per turn; the identity block, memory filter, Q&A filter, and
  capability catalog all derive from that one resolution.

## Migration (non-destructive, opt-in)

1. **Schema:** add the five tables (account, account_identity, space,
   space_member, account_setting) + nullable columns
   (`chat_user.account_uuid`, `memory_claim.space_uuid`,
   `chatroom.space_uuid`, profile owner/space). Nothing existing changes
   meaning: NULL space = global, exactly what the data meant pre-migration.
2. **Backfill:** create the admin account from the existing human `chat_user`
   row; create its personal space; point `account.profile_uuid` at the
   current `profile.current` value if set. All knowledge rows keep NULL
   (global) — the admin sees everything, as before.
3. **`single_user_mode` on:** app behaves exactly as today (no login, admin
   context implied everywhere). Every phase below ships dark behind it.
4. **Flip when ready:** operator configures OAuth env vars, links their
   Google identity from /settings while on localhost, turns
   `single_user_mode` off. From then on, web requires login.

Rollback at any point = flip `single_user_mode` back on; the added columns
are inert in that mode.

## Build order

1. **Phase A — identity spine:** tables, backfill, request context
   (`g.account`), `require_role` decorator wired but permissive under
   `single_user_mode`. Tests: role matrix per route tier.
2. **Phase B — web OAuth:** Authlib, login/logout/pending pages, admin
   approval UI, SECRET_KEY enforcement. Tests: login rules 1–4, fail-closed
   pending.
3. **Phase C — connector identities:** shared resolver, Telegram bridge
   resolves per-sender, guest auto-provisioning, `connector.guest_policy`;
   Discord bridge as a sibling service. Tests: unknown sender → guest;
   linked admin keeps elevation; `closed` policy drops unknowns.
4. **Phase D — spaces & knowledge:** space tables + admin UI,
   `memory_claim.space_uuid` + filter, per-space Q&A overlays + filtered
   retrieval, per-account settings, per-sender identity block + `<requester>`.
   Tests: cross-user leak checks (Ada's private claim never in Grace's
   prompt), overlay precedence, shield isolation.
5. **Phase E — role-scoped capabilities:** per-role assistant catalogs,
   member write-subset configuration, guest read-only set. Tests: guest
   catalog contains no write actions; dispatch rejects anyway (both layers).

Each phase is independently shippable behind `single_user_mode`; the demo-era
features (`profile.current`, global overlay) keep working until their
per-account successors land, then are superseded in place.

## Risks / open questions

- **LlamaIndex metadata filtering** for the Q&A table needs a spike: if
  metadata filters prove awkward, fall back to one pgvector table per space
  (the sync code already rebuilds tables wholesale, so this is contained).
- **Guest memory growth:** open guest policy on a public Discord server could
  accrete many guest accounts/spaces. Mitigation: `connector.guest_policy`
  defaults to `allowlist`, and guests are cheap rows until they chat.
- **Two humans in one room** works for attribution today (distinct
  sender_uuids) but the assistant's reply targeting ("you") may need prompt
  work when rooms get busy — out of scope here, noted for the room roadmap.
- **Backups** now contain multiple people's private spaces; the existing
  age-encryption threat model still holds (one operator-held key). Per-user
  export ("give me my data") is a later, separate feature.
