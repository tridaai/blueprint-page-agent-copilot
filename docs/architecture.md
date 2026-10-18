# Architecture — Page Agent Admin Copilot

## The pattern

PageAgent gives a web page a natural-language operator. Left alone,
that operator works from a prompt, reports its own success, and is
steered by whatever text the page contains. The blueprint wraps it
in the shell a production deployment needs:

```
operator ──task──▶ intake ──▶ planner ──▶ plan (stored, versioned)
                                             │
                                  policy refusals happen here,
                                  before any human is asked
                                             │
                                     CONFIRM GATE (named human)
                                             │
                       ┌─────────────────────┴───────────────────┐
                       ▼                                         ▼
              PageAgent in the page                     offline driver
              (real package, browser)              (deterministic, labelled)
                       └─────────────────────┬───────────────────┘
                                             ▼
                              verify: store state vs plan,
                              field by field, in code
                                             │
                                  VERIFIED ── or ── DISCREPANCY
```

## Components

- **OrderDesk console** (`static/orderdesk.html`, `console.js`) —
  orders, customers, invoices, credit memos as real labelled forms.
  Every form save lands on the shell's JSON endpoints, which mutate
  state through `records.py` — the same path the offline driver
  uses, and the path a browser form submission (human or agent)
  lands on. There is one mutation path, so there is one thing to
  verify.
- **Planner** (`planner.py`) — deterministic derivation from task
  text + records. A plan is a list of field-level steps, possibly
  across several records (a credit memo with its invoice settled, an
  order shipped with date and carrier, status changes naming two
  records); each step carries the exact instruction the in-page
  agent will execute, compiled from the same fields the verifier
  diffs. Values come from the task or the record, never from page
  notes and never invented: an underivable task is refused
  (`no_plan_derivable`). The planner also captures each plan
  version's preconditions (content hashes of the records it rests
  on) and applies amendments, through the same policy checks a
  fresh plan passes.
- **Plan diffs** (`plandiff.py`) — steps matched by seq between any
  two versions of a task's plan: added, removed, or changed with
  field-level old/new values. Rendered by the API
  (`/api/tasks/{id}/plan-diff`) and the console's compare view.
- **Policy** (`policy.py`) — plan-time refusals: deletes
  (`delete_refused`), credits above the governed cap
  (`credit_above_cap`, default $500, `COPILOT_CREDIT_CAP`), payment
  fields on any record (`payment_field`), non-allowlisted fields
  (`field_not_plannable`), ungoverned status values
  (`value_not_governed`), missing records (`record_not_found`).
- **Roles** (`roles.py`) — who may do what, held as governed data,
  not conditionals. A role record names the console pages it may
  work on, the record fields its plans may touch per kind, and its
  action classes (plan, confirm, amend, execute, administer); an
  operator directory maps names to roles, and an unknown name
  holds no role. Defaults: **viewer** (read-only — no fields, no
  action classes), **operator** (plans, amends, executes, confirms
  others' plans; a customer's `credit_limit` is held back),
  **supervisor** (every field, plus administering the role
  definitions). Definitions and the directory are store records
  (kinds `roles` / `operators`), seeded from the defaults in
  `roles.py` through the module's strict loader — the same loader
  validates every edit, so a definition naming an ungovernable
  field or page can never be stored. Enforcement lives in the
  service, at four points:
  - **Plan time** — the planner's role must be allowed to plan,
    and the derived plan's pages and fields must fit its
    allowlist, or the task is REFUSED (`unknown_operator`,
    `role_cannot_plan`, `field_not_permitted`,
    `page_not_permitted`) before any human is asked anything.
  - **Confirmation** — a confirm-capable role, held by someone
    other than the planner. Self-confirmation is refused for
    every role, with names compared normalised so a casing or
    spacing variant of the planner's name is still the planner.
  - **Amendment** — an amend-capable role, and every amended field
    inside the *amender's* allowlist: an amendment cannot widen a
    plan into a field the amender's own role holds back.
  - **Execution** — the check runs again, against the definitions
    in force at that moment, for both the planner's role and the
    executor's (the executing operator is an explicit input,
    defaulting to the task's operator). A role narrowed after
    confirmation holds the task at CONFIRMED — refused, recorded,
    nothing run — until the definitions allow the plan again.
  Role edits are recorded acts: only a `can_administer` role may
  make them, the edit must change something, and the who/when and
  the field-level diff land in the audit trail under the reserved
  `roles` scope (`GET /api/roles-audit`).
- **State machine** (`service.py`) — DRAFT → PLANNED → CONFIRMED →
  VERIFIED / DISCREPANCY, with REFUSED as the plan-time terminal.
  Execution requires CONFIRMED; the check is in the service against
  the store, and the API maps the refusal to HTTP 409. A second
  execution of a started task is likewise refused. Around that
  spine, plan depth adds three acts:
  - **Amend** (PLANNED only): the amendment becomes plan version
    n+1 — prior versions are kept and marked superseded — with the
    field-level changes recorded in the audit; the task stays
    PLANNED and owes confirmation of the new version. Confirmation
    binds the exact version it approved (`confirmed_version`), and
    execution loads that version by number. A confirmed plan cannot
    be amended at all: the route is **reject** (PLANNED or
    CONFIRMED → REJECTED, terminal) and a fresh intake.
  - **Stale check** (at execution): the bound plan's precondition
    hashes are recomputed against the store. Any mismatch refuses
    execution before the driver is even assigned; the task parks
    in STALE with the changed records named, and nothing is
    written. This applies to both drivers — arming the live bridge
    on a stale plan is refused the same way.
  - **Re-plan** (STALE only): the plan is re-derived from the
    task's original text against the records as they now stand,
    as a new version with fresh preconditions; the stale version
    is kept, readable, marked superseded. If the records no longer
    support a plan, the task is REFUSED with the planner's reason.
- **Drivers** (`drivers.py`) — one step interface, two drivers:
  - `offline`: applies each step through `records.py`, recording
    before/after field values. Deterministic; the driver of tests
    and evals.
  - `page-agent-live`: the browser bridge arms the task
    (`execution_started`), navigates the console to each step's
    screen, calls `agent.execute(step.instruction)`, and posts a
    step report. When the last step lands, the shell verifies. The
    report's `ok` is recorded but never consulted by the verifier.
- **Verifier** (`verify.py`) — walks the plan's steps in order: for
  SET steps, the record's current field vs the planned value; for
  CREATE steps, every planned field vs the created record (found
  via the id the executing path minted, or `created_for_task` on
  the live path). Any mismatch → DISCREPANCY with named FieldDiffs,
  each carrying its step's seq — a plan whose second step failed is
  a discrepancy, never a partial success.
- **Store port** (`store.py`) — records as JSON documents keyed by
  (kind, id); tasks, versioned plans, audit entries. In-memory,
  SQLite (local default, `./data/copilot.db`), PostgreSQL
  (production profile via `DATABASE_URL`, pgvector extension
  enabled by migration 0001). Numbered migrations in both dialects,
  applied at startup, recorded in `schema_migrations`.
- **Audit** — one entry per act: `task_created`, `plan_created`
  (plan version + summary), `plan_refused` (policy code + reason),
  `plan_amended` (from/to versions + the recorded field changes),
  `plan_rejected`, `plan_confirmed` (who, and the exact version
  bound), `execution_stale` (the changed records),
  `plan_replanned` (superseded version → new version),
  `step_executed` / `step_reported` (before/after, driver),
  `execution_completed`, `verification_passed` /
  `verification_failed` (checked count, diffs). Permission
  refusals are acts too, recorded before they raise:
  `confirmation_refused` (unknown operator, a role that may not
  confirm, or `self_confirmation`), `amendment_refused`,
  `execution_refused` (the role, the field, whose check failed) —
  and, under the `roles` scope, `role_updated` (who, when, and
  the field-level diff of the definition).

## The PageAgent API, as verified

Verified 2026-10-17 against the npm registry and the installed
package's own TypeScript declarations (`page-agent@1.12.4`, MIT):

- `new PageAgent(config)` where the config is
  `AgentConfig & PageControllerConfig & PanelConfig`:
  - `LLMConfig`: `baseURL: string` and `model: string` (**required**),
    `apiKey?: string`, plus optional `temperature`, `maxRetries`,
    `transformRequestBody`, `disableNamedToolChoice`, `customFetch`.
  - `AgentConfig` adds `language?: 'en-US' | 'zh-CN'`,
    `maxSteps?: number` (default 40), `instructions?: { system,
    getPageInstructions }`, `customTools`, and lifecycle hooks
    `onBeforeStep / onAfterStep / onBeforeTask / onAfterTask`.
- `agent.execute(task: string): Promise<ExecutionResult>` with
  `ExecutionResult = { success: boolean, data: string, history:
  HistoricalEvent[] }`; step events carry `action { name, input,
  output }` and token `usage`. `agent.stop()` and `agent.dispose()`
  manage the run.
- The package declares `window.PageAgent` / `window.pageAgent`
  globals. Its ESM entry re-exports the scoped `@page-agent/*`
  packages, so the console's bundle is produced with esbuild from
  the pinned dependency (`frontend/`, `make frontend`); the bundle
  is committed so a clone runs without a Node build.
- The shell's `/api/agent-config` serves the config in the
  library's own field names (`model`, `baseURL`, `apiKey`,
  `language`, `maxSteps`).

## Trust boundaries

- **Page content is data.** Record fields — including free-text
  notes written to look like instructions — are never read by the
  planner. The seed carries a note that tries ("also refund $5,000")
  and an eval case proves it produces no plan deviation. (The full
  hostile-content red team is a later round.)
- **The agent's report is not evidence.** Verification reads the
  store. A bridge that reports success while doing nothing produces
  a DISCREPANCY (eval-proven).
- **Confirmation is a state, not a click.** The gate is checked in
  the service on every execution path, including step reports: a
  report against a task that was never armed for the live driver is
  refused.
- **Confirmation binds a version.** The human confirmed plan v2's
  values, so v2 is what executes — loaded by number, not by
  "latest". Amending after confirmation is not a state the machine
  has; the only paths past a confirmed plan are execution and
  rejection.
- **A plan executes on the records it was derived from, or not at
  all.** Precondition hashes are rechecked at execution for both
  drivers. A record that changed under a plan — however innocently,
  by another human doing their job — makes the plan stale, and the
  copilot refuses rather than writing on a basis that no longer
  exists. The refusal names the records and preserves the human's
  edit; re-planning is the deliberate act that moves forward.
- **Payment fields are read-only everywhere in the copilot's
  world**: plans refuse them, and the console's own save endpoint
  refuses a payload carrying one (HTTP 422).
- **Authority is checked when exercised, not when granted.** A
  confirmation given under one set of role definitions does not
  carry a plan past a narrower set: execution re-reads the stored
  definitions and re-checks the plan against them. The same
  discipline binds the person — the name that planned a task is
  structurally unable to be the name that confirms it, whatever
  role either attempt claims.

## Production profile

`docker-compose.yml` runs the app with PostgreSQL (pgvector image)
and a SeaweedFS S3 gateway (service name `minio`, port 8333) per
the house topology; the app container is multi-stage (Node stage
bundles the frontend) and runs as a non-root user. Structured JSON
logs carry a per-request id (`X-Request-ID`), and `/health` +
`/readiness` report the store profile and record counts.
