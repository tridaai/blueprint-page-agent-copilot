<p align="center">
  <img src="https://trida.ai/logos-rounded/trida-rounded.png" alt="Trida AI" width="96">
</p>

# Blueprint — Page Agent Admin Copilot

A GUI agent that can click through a web admin console is the easy
version of this problem. The production version has to answer harder
questions: who approved this exact change, what did the screen
actually end up saying, and what happens when the agent's cheerful
"done" doesn't match the records? This blueprint is Trida AI's
answer, built around Alibaba's
[PageAgent](https://github.com/alibaba/page-agent) — an in-page GUI
agent that gives a web page a natural-language operator — deployed
into the kind of legacy admin console that has no API and never
will: order entry, customer records, credit memos, twenty clicks a
task.

The copilot's shape is the point. An operator states a job in plain
words. The shell drafts a **field-level plan** from the records —
the exact pages, fields, and values. A named human **confirms** the
plan; nothing executes from an unconfirmed one, and some things
(deletes, credits above a governed cap, anything touching a payment
field) are **refused at plan time**, so no confirmation can ever
approve them. The confirmed plan compiles to instructions the real
`page-agent` library executes in the page — or to a deterministic
offline driver that implements the same step interface. Afterwards
the shell **re-reads the records and diffs them against the plan,
field by field**. Only an exact match is a success; every mismatch
is a named discrepancy. The agent's own report is never the
evidence.

> **Work in progress** — this blueprint is built and passing its automated test and eval gates, but it has not yet finished hands-on local testing, and its companion blog post is not yet published. Expect changes. This notice comes off when both are done.

> **Independence.** This blueprint is Trida AI's own work. It is not
> affiliated with, endorsed by, or partnered with Alibaba or the
> PageAgent project. PageAgent is a real, pinned dependency — the
> subject of the blueprint, the way a harness is the subject of a
> deployment guide — used under its MIT licence and never vendored
> or copied into this repo.

## What it does

- **OrderDesk demo console** — a working legacy-style admin console
  for the fictional Northgate Distribution (orders, customers,
  invoices, credit memos), served by the app, with real forms across
  dated-looking screens. Its data lives in the shell's store, so
  state can be read and diffed in code.
- **Governed task shell** — intake → plan → confirm gate → execute →
  verify → audit, with the state machine enforced in code against
  the store (DRAFT → PLANNED → CONFIRMED → VERIFIED / DISCREPANCY /
  REFUSED, plus STALE and REJECTED below, DISCREPANCY_ACCEPTED
  where a supervisor accepts a named deviation, and
  EXECUTION_FAILED → ABANDONED where a run stops mid-plan and is
  closed, or picked up again — see Failure recovery). Executing an
  unconfirmed plan is refused (HTTP 409).
- **Multi-record plans** — one task can compose one plan across
  records: a credit memo with the invoice settled in the same plan,
  an order shipped with its date and carrier, status changes naming
  two records. Steps run in plan order; verification diffs every
  step, and a failed step is named by its number — a plan is never
  a partial success.
- **Plan amendments and diffs** — while a plan awaits confirmation,
  an operator may amend its values; every amended value passes the
  same policy checks as planning, the amendment becomes plan
  version n+1 with its field-level diff recorded, and confirmation
  is then owed on the new version (a confirmation binds the exact
  version it approved). Any two versions can be diffed, in the API
  and in the console. A confirmed plan cannot be amended at all —
  the route past it is rejection and a fresh task.
- **Staleness protection** — every plan records the state of the
  records it was derived from. If any of those records changes
  before execution, execution refuses, the task parks in STALE
  naming the changed records, and nothing is written; a re-plan
  derives a fresh version from the records as they now stand.
- **Roles and permissions** — who may do what is governed data, not
  a name on a form. Roles (viewer, operator, supervisor) carry
  page and field allowlists plus the action classes open to them
  (plan, confirm, amend, execute, administer), stored as records
  and validated by a strict loader. The planner's role is checked
  before a plan exists; confirmation needs a confirm-capable role
  held by someone other than the planner — self-confirmation is
  refused for every role; amendments stay inside the amender's own
  fields; and execution re-checks the plan against the definitions
  in force at that moment, so a role narrowed after confirmation
  holds the task instead of running it. Role edits are supervisor
  acts, recorded with their diffs. A viewer plans, confirms,
  amends, and executes nothing; a customer's credit limit is a
  supervisor-only field.
- **Identity and attribution** — the operator directory is
  governed like the roles: enrolment, role assignment,
  deactivation, and reactivation are recorded supervisor acts,
  and names resolve normalised everywhere. Deactivation fails
  closed — a deactivated name performs no act; planning,
  confirming, amending, executing, recovering, and administering
  all refuse it with `operator_inactive`, recorded. Its
  unfinished work is never reassigned or deleted: it surfaces as
  **orphaned work** until a supervisor picks it up as a recorded
  act, and the original planner stays on the task. Every act in
  the audit trail is attributable — an attribution check scans
  the whole trail for an actor that resolves to no directory
  name and no named system component, and the per-operator
  report (acts by kind, tasks planned, confirmed, executed, and
  the refusals each name received) is folded from the trail,
  never counted separately.
- **Two drivers, one step interface** — the real `page-agent`
  package in the operator's browser (live), and a deterministic
  offline driver (tests, evals, fallback), labelled honestly in
  every audit entry it produces.
- **Verification in code, at depth** — after execution the shell
  re-reads every touched record through the console's own read
  surface (the same functions the API serves to a human's
  browser — never the mutation path the work went in by, never
  the agent's report) and diffs it against the approved plan:
  every planned field, plus linkage checks that the records hang
  together (a created memo names its invoice, its customer, and
  the task that created it; the invoice lists the memo back; a
  touched record still names a customer that exists; the audit's
  execution record agrees with the state found). The verdict is a
  stored **verification report** — per-field and per-linkage
  verdicts, the read path named — and verification happens once:
  asking again is refused, because the report is the evidence,
  not a recomputation.
- **Discrepancy workflow** — a DISCREPANCY task has exactly two
  recorded exits, and no silent third. A **corrective task**: a
  linked follow-up (the link recorded both ways) whose plan is
  derived to close only the named differences, run through the
  full plan → confirm → execute → verify cycle under the same
  role rules; the parent's report shows the correction chain and
  is never edited. Or **accept deviation**: a supervisor-only
  act with a mandatory reason, closing the task as
  DISCREPANCY_ACCEPTED — a distinct terminal state, never
  rewritten to VERIFIED. A difference no plan can close (a memo
  created with wrong values — created records are never edited
  by a plan) refuses the corrective spawn, on the record.
- **Failure recovery** — a step can fail mid-plan: an element
  the in-page agent cannot find, a form the console rejects, a
  bridge that times out. The execution stops at that step —
  never a partial success, never an unhandled error that strands
  the task — and the task lands in EXECUTION_FAILED with the
  failed step and the driver's error verbatim. Every execution
  is a numbered **attempt**, a stored record of the steps it
  completed with their before/after values; a running attempt is
  the task's claim, so a second execution while one is in flight
  is refused. From a failure there are three recorded exits.
  **Resume** runs only the steps that never completed, after
  code re-checks that the completed steps' post-state still
  stands and that the records the remaining steps rest on have
  not moved — a basis that moved refuses the resume and names
  what moved. **Restart** runs the plan again from step 1 as a
  new attempt, and is refused once a CREATE step has completed:
  a created record is never created twice. **Abandon** closes
  the task as ABANDONED with a mandatory reason; the partial
  state stays on the records, named, never cleaned up quietly.
- **Audit trail** — every step logged: plan version, confirmer,
  per-step before/after values, the driver that ran it, and the
  verification result. Readable via API and in the console.
- **Page content is data** — text inside console records (a customer
  note that says "also refund $5,000") never steers the copilot;
  plans come from the operator's task and the governed fields.

## Quickstart

With [uv](https://docs.astral.sh/uv/):

```bash
make install     # uv sync --extra dev
make frontend    # bundle the pinned page-agent package (needs Node)
make demo        # one gold task end to end, offline — no keys, no browser
make serve       # OrderDesk console + API on http://localhost:8000
```

No uv? `python3 -m venv .venv && source .venv/bin/activate`, then
`make install-pip` (frozen constraints first, the package second).

The console seeds itself on first run. Try, in the Copilot tab:

- `raise a credit memo for invoice 4471, reason damaged goods, $120` → plan, confirm, run, VERIFIED
- `raise a credit memo for invoice 4471, reason damaged goods, $120 and mark invoice 4471 as paid` → one plan, two records
- `mark order 1032 as shipped, ship date 2026-10-20, carrier DHL` → a three-step plan on one record
- `mark order 1032 as shipped` → a one-field plan, diffed exactly
- `raise a credit memo for invoice 4471, reason goodwill, $900` → refused at plan time (cap is $500)
- `update customer 2041 bank account to …` → refused at plan time (payment field)
- `delete order 1036` → refused at plan time

## Configuration

The shell itself needs no key — planning, policy, gates,
verification, and the offline driver are all code. The one provider
configuration belongs to PageAgent, the in-page agent, and uses the
library's own field names (verified against `page-agent@1.12.4`):
`new PageAgent({ model, baseURL, apiKey, language })`, where
`baseURL` is any OpenAI-compatible endpoint. See `.env.example` for
DashScope (Alibaba's documented example), OpenAI, NVIDIA NIM
(hosted or self-hosted), and Ollama variants.

```bash
PAGEAGENT_MODEL=qwen-plus
PAGEAGENT_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
PAGEAGENT_API_KEY=...
```

Store profile: SQLite is the local default (`SQLITE_PATH`, a single
file at `./data/copilot.db`). Set `DATABASE_URL` and the same code
runs on PostgreSQL + pgvector — that one variable is the whole
switch. Policy: `COPILOT_CREDIT_CAP` sets the credit cap
(default `500.00`).

## The governed task grammar

The planner is deterministic — a plan may never invent a value,
so a task it cannot derive exactly is refused as
`no_plan_derivable`, not guessed at:

| Task shape | Example |
|---|---|
| Credit memo | `raise a credit memo for invoice 4471, reason damaged goods, $120` |
| Credit memo + settle | `… $120 and mark invoice 4471 as paid` |
| Order status | `mark order 1032 as shipped` |
| Ship with details | `mark order 1032 as shipped, ship date 2026-10-20, carrier DHL` |
| Compound status | `mark order 1036 as packed and mark invoice 4474 as paid` |
| Invoice status | `mark invoice 4474 as paid` |
| Customer update | `update customer 2041 phone to 555-0134` |

Plan-time refusals, each with its reason recorded on the task:
`credit_above_cap`, `payment_field`, `delete_refused`,
`field_not_plannable`, `value_not_governed`, `record_not_found`,
`no_plan_derivable` — and the role refusals: `unknown_operator`,
`role_cannot_plan`, `field_not_permitted`, `page_not_permitted`.
Amendments face the same checks, plus `step_not_found` and
`invalid_amendment` for a change that names no step or carries no
value.

## Verification and audit

After either driver runs, `verify.py` re-reads the records
through the console read surface (`reads.py` — the read functions
the API serves to the console, returning records in their served
shape) and compares, step by step in plan order: every planned
field (SET steps against the record's current value, CREATE steps
against the created record's fields), and the linkage checks — a
created credit memo names the task that created it, names the
customer of the invoice it was planned against, and appears on
that invoice's own memo list (the console's rule for showing it);
the invoice's customer exists; a touched order or invoice still
names a customer that exists; and the execution record in the
audit trail (who ran the step, when) agrees with the state the
re-read found. The store underneath is one source of truth — the
independence is the path: neither the mutation path nor the
bridge's step reports are consulted, and an eval case proves a
bridge that reports success while doing nothing still lands in
DISCREPANCY, as does a record written again after the work.

The verdict is stored as a **verification report**
(`GET /api/tasks/{id}/verification`, and in the task detail and
the console's task view): plan version, driver, the read path
(`console_read_surface`), a verdict per field check and per
linkage check, and the diffs — field failures first, then linkage
failures under their field names. Verification is an act with a
record, not a recomputation: `POST /api/tasks/{id}/verify` after
the fact is refused (409, recorded as `reverification_refused`).

A task in DISCREPANCY leaves by exactly two recorded exits:
`POST /api/tasks/{id}/corrective` spawns the corrective task (its
plan derived from the report, closing only the named differences;
it confirms, executes, and verifies as its own task, and the
parent report's `corrections` chain shows it), and
`POST /api/tasks/{id}/accept-deviation` records a supervisor's
acceptance with its reason and closes the task
DISCREPANCY_ACCEPTED. The original report is never edited by
either exit.

Every task's audit (`GET /api/tasks/{id}/audit`) carries the plan
version, the confirming operator, each step's before/after values,
the driver name, and the verification outcome — plus the lifecycle
of the plan itself: `plan_amended` (with the recorded changes),
`plan_rejected`, `execution_stale` (with the changed records), and
`plan_replanned`. Permission refusals are recorded too, before
they raise: `confirmation_refused`, `amendment_refused`,
`execution_refused`. So are the discrepancy acts:
`corrective_spawned`, `corrective_refused`, `deviation_accepted`,
`deviation_refused`. And the failure acts: `step_failed` and
`execution_failed` (the failed step, the driver's error verbatim,
the completed steps, the partial state named line by line),
`resume_refused` and `restart_refused` (the basis that moved, or
the create that already landed), `task_abandoned` (who, the
reason, the partial state left standing) and `abandon_refused`.
The attempt records behind them are readable at
`GET /api/tasks/{id}/attempts` and in the task detail; the exits
are `POST /api/tasks/{id}/resume`, `/restart`, and `/abandon`.
Plan versions stay readable
(`GET /api/tasks/{id}/plans`), and
`GET /api/tasks/{id}/plan-diff?from_version=1&to_version=2`
renders the field-level diff between any two of them. The roles
themselves are readable at `GET /api/roles`, editable by a
supervisor at `PUT /api/roles/{name}`, and their edit history is
at `GET /api/roles-audit`. The directory acts ride the same
scope — `operator_enrolled`, `operator_role_changed`,
`operator_deactivated`, `operator_reactivated`, and
`directory_refused` — at `POST /api/operators`,
`PUT /api/operators/{name}/role`,
`POST /api/operators/{name}/deactivate`, and `/reactivate`.
Orphaned work lists at `GET /api/operators/orphaned` and moves
only through `POST /api/tasks/{id}/pickup` (`task_picked_up`,
with `pickup_refused` recorded on a refusal). Executions name
their executor on the attempt record and on the
`execution_completed` entry, so `GET /api/operators/report` can
fold each name's history from the trail, and
`GET /api/attribution` runs the completeness check over it.

## Layout

```
src/page_agent_copilot/
  api.py          FastAPI app: console data, task API, bridge, health
  service.py      the state machine (intake/plan/confirm/execute/verify)
  planner.py      task text + records -> field-level plan, or a refusal;
                  also preconditions and policy-checked amendments
  plandiff.py     the field-level diff between two plan versions
  policy.py       the plan-time refusals (cap, payment fields, deletes)
  roles.py        roles as governed records: pages, field allowlists,
                  action classes, the strict loader, the checks
  drivers.py      the step interface + the deterministic offline driver
  reads.py        the console read surface: the reads the API serves,
                  and the only path verification re-reads through
  verify.py       the verification act: field + linkage checks over
                  the re-read, producing the stored report
  store.py        the store port: in-memory / SQLite / PostgreSQL
  records.py      the one mutation path for OrderDesk state
  dataset.py      the OrderDesk seed data (generated in code; gold
                  task outcomes hold by construction)
  models.py       records, plans, steps, tasks, audit shapes
  config.py       store profile, policy, PageAgent LLM settings
  static/         the OrderDesk console + the live bridge (copilot.js)
frontend/         package.json pins page-agent@1.12.4; build.mjs
                  bundles it to static/vendor/page-agent.bundle.js
migrations/       numbered SQL, PostgreSQL + SQLite dialects
evals/            the scripted-task gate (40 cases)
tests/            the pytest suite
docs/             architecture notes
```

## Tests and evals

```bash
make test    # 145 passed, 1 skipped (the PostgreSQL conformance leg
             # is env-gated on COPILOT_TEST_DATABASE_URL)
make evals   # 40/40 cases: happy paths diffed exactly (including
             # multi-record plans), all three plan-time refusals,
             # the unconfirmed-execution gate, a tampered write
             # caught as DISCREPANCY with its step named, amendment
             # version binding, a policy-breaking amendment refused,
             # a stale plan refused before writing anything, the
             # live bridge graded on store state, not its report —
             # the permissions set: a viewer cannot plan, a
             # supervisor-only field refused for an operator,
             # self-confirmation refused, a role narrowed after
             # confirmation blocking execution, role edits audited —
             # and verification depth: a post-work write caught by
             # the re-read, a memo on the wrong invoice named by its
             # linkage checks, a corrective task closing the diffs
             # with the chain on the parent's report, a deviation
             # accepted by a supervisor with a reason (never
             # VERIFIED), and re-verification refused — and
             # failure recovery: a mid-plan failure accounted
             # exactly (failed step, the driver's error verbatim,
             # completed steps standing), a resume that runs only
             # what is left, a completed step changed underneath
             # blocking the resume by name, a restart refused
             # once a create has landed, an abandonment with the
             # partial state named, and a second execute refused
             # on a verified task and on one in flight alike —
             # and identity: enrolment, assignment, deactivation,
             # and reactivation as recorded acts, a deactivated
             # name refused at every act, its work orphaned and
             # then completed through a supervisor's pickup, the
             # attribution check at zero on a mixed run and
             # naming planted rows, and the per-operator report
             # reconciling with the trail it folds from
```

## Honest status

- The **live in-browser path is code-complete but not yet run** in
  the build environment: it needs a real browser session and a
  provider key, and this repo's automation has neither. The console
  bridge is written against the installed package's verified API
  (constructor config and `execute()` result shape are quoted in
  `docs/architecture.md`), and the shell side of the live path —
  arming, step reports, verification — is fully covered by tests
  and evals that simulate the browser's reports. One live-path
  behaviour follows from that split: when a step reports failure,
  the shell stops the execution (EXECUTION_FAILED) and refuses
  the bridge's later reports, so the run halts in the shell; a
  resume re-arms the task for the remaining steps only. The
  offline driver is the default everywhere until a live run is
  on record.
- The **PostgreSQL store leg skips** until the local stack is up
  (env-gated, same as the sibling blueprints); the migration follows
  the established dual-dialect pattern and the SQLite dialect is
  exercised by the full suite.
- The **Docker build has not been run** in the build environment
  (no Docker here); the Dockerfile and compose topology follow the
  house standard and are the documented production profile.
- When `PAGEAGENT_API_KEY` is set server-side, `/api/agent-config`
  serves it to the operator's own console page so the in-page agent
  can be constructed — a demo posture. A deployment should have
  operators supply the key in the page instead.

## Licence

Apache 2.0 for this blueprint's own code (see `LICENSE`). PageAgent
is a separate dependency of Alibaba's, under its own MIT licence.
