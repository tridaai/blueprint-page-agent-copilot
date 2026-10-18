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
                         verify: re-read via the console read
                         surface — field + linkage checks, in code
                                             │
                       VERIFIED ── or ── DISCREPANCY (stored report;
                       two exits: a corrective task, or a supervisor
                       accepting the deviation)
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
  VERIFIED / DISCREPANCY, with REFUSED as the plan-time terminal
  and DISCREPANCY_ACCEPTED as the deviation terminal (reached only
  through the supervisor acceptance above; a DISCREPANCY otherwise
  leaves only by spawning a corrective task, which is a new task,
  not a transition of this one). Execution requires CONFIRMED; the
  check is in the service against the store, and the API maps the
  refusal to HTTP 409. A second execution of a started task is
  likewise refused. An execution whose driver step fails mid-plan
  lands in EXECUTION_FAILED, which leaves only by the recovery
  acts below (resume, restart, abandon — abandon closing it as
  ABANDONED). Around that spine, plan depth adds three acts:
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
  And failure recovery adds its three, under **Attempts and
  failure recovery** below: **resume** and **restart** (both
  EXECUTION_FAILED only, each a new numbered attempt) and
  **abandon** (EXECUTION_FAILED → ABANDONED, with a reason).
- **Drivers** (`drivers.py`) — one step interface, two drivers:
  - `offline`: applies each step through `records.py`, recording
    before/after field values. Deterministic; the driver of tests
    and evals.
  - `page-agent-live`: the browser bridge arms the task
    (`execution_started`), navigates the console to each step's
    screen, calls `agent.execute(step.instruction)`, and posts a
    step report. When the last step lands, the shell verifies. The
    report's `ok` is recorded but never consulted by the verifier.
  A step from either driver can fail — the outcome carries `ok`
  False and the driver's error verbatim (for the live bridge, a
  step report with `ok` False). The shell stops the execution at
  that step; nothing after it runs. The offline driver carries a
  `fail` hook beside `tamper` — a map of step seq to the error to
  fail with — so the eval pack can arm the failures a real console
  produces (an element not found, a form rejected, a timeout)
  deterministically.
- **Console read surface** (`reads.py`) — the read functions the
  API's console GET endpoints serve from: records in their served
  (JSON) shape, plus the console's own derivations (an invoice's
  memo list is the memos scanned for its id — the rule
  `console.js` renders by). One read path for the human's browser
  and for verification, so what is diffed is what a human would
  see. It mutates nothing.
- **Verifier** (`verify.py`) — runs once, at the end of execution,
  and re-reads every touched record through the read surface —
  never through the mutation path (`records.py`) the work went in
  by, and never consulting the bridge's step reports or the
  driver's outcomes. Two check families, one verdict:
  - **Field checks**: the plan's steps in order — for SET steps,
    the record's current field vs the planned value; for CREATE
    steps, every planned field vs the created record (found via
    the id the executing path minted, or `created_for_task` on
    the live path).
  - **Linkage checks**: whether the records hang together by the
    console's own rules. A created memo names this task
    (`created_for_task`), names the customer of the invoice it
    was planned against (stamped at creation in `records.py`
    from that invoice — so a memo issued against the wrong
    invoice names the wrong customer, and the check catches it
    by name), and appears on that invoice's memo list; the
    invoice's customer exists; a touched order or invoice still
    names a customer that exists; and the execution record in
    the audit trail — who ran each step, when — agrees with the
    state the re-read found (for the live bridge, whose report
    is advisory, presence is all that is checked).
  Any failure → DISCREPANCY with named FieldDiffs (field failures
  first, in plan order, then linkage failures under their field
  names), each carrying its step's seq — a plan whose second step
  failed is a discrepancy, never a partial success. The verdict is
  stored as a **verification report** (a store record, kind
  `verifications`): plan version, driver, the read path
  (`console_read_surface`), every check with its verdict, and the
  diffs. It is served at `GET /api/tasks/{id}/verification`, in
  the task detail, and in the console's task view, where the
  report also carries the task's correction chain, composed at
  read time. Verification is an act with a record, not a
  recomputation — a second run is refused
  (`service.verify_task`, `reverification_refused` in the audit).
  The independence boundary, stated plainly: the store is one
  single source of truth, shared by design; what is independent
  is the path in — a mutation-path bug or a lying bridge cannot
  certify itself because neither is read from.
- **Discrepancy exits** (`service.spawn_corrective`,
  `service.accept_deviation`; derivation in
  `planner.build_corrective_steps`) — a DISCREPANCY task has
  exactly two, both recorded, and no silent third:
  - **Corrective task**: a new task linked both ways (its
    `parent_task_id`; the parent's `corrective_task_ids`) whose
    plan is derived from the parent's verification report —
    only the failed steps, only the parent plan's values. SET
    steps re-run as planned; a CREATE step whose record never
    landed is re-issued. The corrective goes through the full
    cycle (second-person confirmation, execution, its own
    verification), under the role rules: the spawner is its
    planner, and the plan must fit the spawner's role — a
    corrective for a supervisor-only field is a supervisor's to
    spawn. The parent stays DISCREPANCY; its stored report is
    never edited, and the chain (`corrections`) is composed onto
    it at read time. A second spawn after one corrective has
    verified is refused (`already_corrected`). And some
    differences no plan can close — a memo created with wrong
    values (created records are never edited by a plan; a second
    memo would double the fact), a linkage field no plan may
    set: derivation refuses (`corrective_cannot_close`, recorded
    as `corrective_refused`), and the remaining exit is the
    acceptance below.
  - **Accept deviation**: a supervisor act (in role terms, a
    role with `can_administer`) with a mandatory reason. The
    task closes as DISCREPANCY_ACCEPTED — a distinct terminal
    state, never rewritten to VERIFIED — with who and why on the
    task, and the accepted diffs (from the stored report) in the
    `deviation_accepted` audit entry. The report stands as
    written.
- **Attempts and failure recovery** (`service.py`) — every
  execution is a numbered **attempt**, a stored record (kind
  `executions`): the driver, the plan version, the mode
  (`execute`, `resume`, `restart`), every step outcome it
  produced with before/after values, and — on failure — the
  failed step, the driver's error verbatim, and `state_lines`
  naming the state the attempt left in place. An attempt still
  `running` is the task's execution claim: the claim is taken
  under the service lock, so a second execute/resume/restart
  while one is in flight is refused (`attempt_in_flight`,
  recorded) — for the offline driver an attempt finishes inside
  its call, and for the live bridge it spans the step reports,
  which is exactly the in-flight window. When a step fails, the
  attempt closes as failed and the task lands in
  EXECUTION_FAILED with the failed step and error mirrored on
  it; no verification runs, nothing is rolled back, and the
  completed steps stand. From there, three recorded acts, each
  decided in code before anything runs:
  - **Resume**: run only the steps that never completed, as a
    new attempt under the failed attempt's driver (a live resume
    re-arms the bridge for the remaining steps). First, two
    basis checks (`service._basis_problems`): every completed
    step's post-state still stands — a SET step's field still
    holds the value it left, a CREATE step's record still exists
    with its planned fields — and every record the remaining
    steps rest on still hashes to the plan's preconditions,
    except records a completed step itself wrote (covered by
    the first check). A moved basis refuses the resume
    (`resume_refused`, `basis_moved`) and names what moved: the
    shell never writes over a change it cannot account for. The
    executor's role is re-checked exactly as at execution. A
    resumed execution verifies over the whole plan, gathering
    created records from every attempt the execution counts.
  - **Restart**: run the plan again from step 1, as a new
    attempt — refused once any CREATE step has completed
    (`restart_refused`, `create_already_applied`), because a
    created record is never created twice; a plan in that state
    can only be resumed. The same role and basis checks run
    first: the state must still be exactly the plan's
    preconditions plus the completed steps' writes, so a
    restart's SET steps re-apply their own values onto state
    that provably has not moved.
  - **Abandon**: an operator whose role may execute closes the
    task as ABANDONED, with a mandatory reason. The partial
    state is not cleaned up: the completed steps' writes stay on
    the records, named in the final attempt's state lines and in
    the `task_abandoned` audit entry, and the task records who
    abandoned it and why.
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
  `verification_failed` (checked count, the read path, diffs).
  Permission refusals are acts too, recorded before they raise:
  `confirmation_refused` (unknown operator, a role that may not
  confirm, or `self_confirmation`), `amendment_refused`,
  `execution_refused` (the role, the field, whose check failed) —
  and, under the `roles` scope, `role_updated` (who, when, and
  the field-level diff of the definition). The verification-depth
  acts join them: `reverification_refused` (a second verification
  asked for; the recorded report stands), `corrective_spawned`
  (the corrective task's id and the diffs it closes),
  `corrective_refused` (no plan can close the difference, the
  spawner's role does not fit, or the differences are already
  corrected), `deviation_accepted` (who, the mandatory reason,
  the diffs accepted), and `deviation_refused` (not a supervisor,
  or no reason given). Failure recovery adds: `step_failed` (the
  step, the attempt, the driver's error verbatim),
  `execution_failed` (the attempt, the completed steps, the
  partial state named line by line), `execution_started` for a
  resumed or restarted attempt (its mode and the steps it will
  run), `resume_refused` / `restart_refused` (the basis problem
  or the landed create that decided it), `task_abandoned` (who,
  the mandatory reason, the partial state), `abandon_refused`,
  and `execution_refused` with code `attempt_in_flight` (a second
  execution claimed while one was running).

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
- **The agent's report is not evidence.** Verification re-reads
  the records through the console read surface. A bridge that
  reports success while doing nothing produces a DISCREPANCY
  (eval-proven), and so does a record written again after the
  work — the verdict is on the state that stands when the re-read
  happens, not on the state anyone claims was left.
- **A discrepancy is never rewritten.** The stored verification
  report is written once. A corrective task is a new task with
  its own report (the parent's shows the chain, composed at read
  time); an accepted deviation closes the task as
  DISCREPANCY_ACCEPTED with the report still naming every
  difference. No path turns a discrepancy into a VERIFIED after
  the fact.
- **A failure is a state, not an exception, and partial work is
  never hidden.** A driver step that fails stops the execution
  and is recorded — the attempt, the task state, the audit — with
  the driver's error verbatim; it never surfaces as an unhandled
  error that strands the task, and never as a partial success.
  What the completed steps wrote stands, named line by line, and
  the only ways forward are the recorded acts (resume, restart,
  abandon), each of which re-checks the world before writing:
  resume and restart refuse a basis that moved underneath the
  completed work rather than writing over it.
- **Verification happens once.** It is an act with a record, run
  at the end of execution; asking for it again is a recorded
  refusal, not a recomputation — a verdict that could be re-run
  until it changed would not be evidence of anything.
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
