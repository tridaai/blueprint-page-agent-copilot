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
- **Identity and attribution** (`roles.py`, `service.py`) — the
  directory itself is governed data with a lifecycle, not a seed
  constant. Enrolment, role assignment, deactivation, and
  reactivation are recorded acts in the same `roles` audit scope,
  each gated on an active `can_administer` holder; names are
  normalised at enrolment and at every lookup, an enrolled name
  cannot be enrolled twice, and the last active administrator
  cannot be deactivated — a directory nobody can administer is a
  lockout, not a governance act. A name is never deleted:
  deactivation is an `active` flag on its entry, so its history
  keeps resolving to it. Deactivation fails closed at every act
  the shell offers — intake answers with a REFUSED task, and
  confirm, amend, execute, resume, restart, abandon, corrective,
  and the deviation acceptance refuse with `operator_inactive`,
  recorded wherever that act records its refusals (reject and
  re-plan, whose refusals were never recorded, refuse plainly).
  Role edits and directory acts refuse a deactivated actor the
  same way. What deactivation does *not* do is move work: a task
  whose operator stands deactivated while it still owes that
  operator an act (PLANNED, CONFIRMED, STALE, EXECUTION_FAILED)
  is **orphaned work**, listed at `GET /api/operators/orphaned`
  and in the console, and it moves only through a supervisor's
  recorded **pickup**, which re-names the task's operator and
  keeps the original planner on the task (`planned_by`,
  `picked_up_from`). The planner-side execution check is
  deliberately not an identity check — it re-checks the plan
  against the planner's role definitions — because a deactivated
  planner's confirmed work is exactly what pickup exists to
  finish. Attribution closes the loop: attempts record their
  executor and the execution audit entries carry it, the store
  port scans the whole trail (`list_all_audit`), and two reads
  fold from it — the **attribution check** (`attribution_check`,
  `GET /api/attribution`), which names any act whose actor is
  missing or resolves to neither a directory name (any state) nor
  a governed system component (`SYSTEM_ACTORS`: `planner`,
  `policy`, `verifier`, and the two driver names), and the
  **per-operator report** (`GET /api/operators/report`), each
  name's acts by kind, tasks planned/confirmed/executed, and
  refusals received by code — folded from the trail on every
  read, never kept as counters, so it cannot drift from what it
  summarises. Two fold details are of their time and documented
  in the code: a plan refusal is written under the `policy`
  actor, so it counts for the task's planner, whom the task
  record names; and executions from before this round carry no
  executor on their entry, so those count for the task's
  operator — the executor `execute` defaulted to then.
- **Task library** (`templates.py`, `service.py`) — the recurring
  task shapes as governed records, following the roles' pattern
  exactly: defaults as data in the module, validated by a strict
  loader (`load_template`) that the seeding path and every
  publish both pass through, stored as records (kind
  `templates`, id `name:v<version>`), and administered in their
  own reserved audit scope (`templates`). A `TaskTemplate` is a
  pattern — task text with named slots — a description of when
  to use it, and a version; the loader holds the pattern's
  placeholders to be exactly the declared slots, a record slot
  to a real record kind, and every pinned example to fill
  exactly the declared slots. **Publishing** a new version is a
  recorded act gated like the directory acts (enrolled, active,
  `can_administer`): the service assigns the next version
  number, identical content is refused as `no_change`, and the
  superseded version is kept — readable and instantiable by name
  and version, never edited or deleted. **Instantiation** is
  deliberately not a planning path: slot values are validated
  first (a record slot must name a record that exists — the
  planner's own `record_not_found` refusal, recorded as
  `template_instantiation_refused` before it raises; a money
  slot must parse; a date slot must be a calendar date), the
  pattern renders to plain task text, and that text goes through
  `intake` unchanged, so role rules, the grammar, and policy
  apply exactly as they do to a typed task — a slot value that
  renders underivable or policy-breaking text returns the
  REFUSED task intake itself produces, with the planner's reason
  on it. The task records `template_name` and the exact
  `template_version` used — the resolved version, named even
  when the caller asked for the current one — and the act lands
  on the task's trail as `template_instantiated`. Each version
  carries **pinned examples** (slot values + the expected plan
  shape: task type and per-step record/field/value or field set,
  in `plan_shape`'s terms). A version's **health is computed on
  read**: `template_health` re-derives every example against a
  scratch store seeded with the OrderDesk records — the same
  basis the eval gate grades on — and the library view folds the
  verdict in (`current`, `health.ok`, per-example detail naming
  the refusal or the first differing step). The eval runner
  stores no results file, so there is no stored verdict to read
  back; health is the examples, re-derived at the moment of
  asking, and the same examples run as cases in the eval gate —
  a planner or policy change that breaks a template fails the
  gate and marks the version broken in the library by name and
  version, which is the seed the template regression gate
  (round 19) grows from.
- **Batches** (`batches.py`, `service.py`) — work submitted
  together, as one governed record (kind `batches`), following
  the same records pattern: the record is written once at
  creation and never edited, and its administration keeps its
  own reserved audit scope (`batches`). A submission is a list
  of members in order, each a task text or a template with its
  slots; creating the batch states every member through the
  ordinary doors — `intake` for a text, `instantiate_template`
  (which itself intakes) for a template member — so the role
  rules and policy govern each member exactly as if it had been
  stated alone, and the batch confers no permission of its own
  (a viewer's batch is a batch of `role_cannot_plan` refusals).
  A member that refuses stays in the batch with its refusal: a
  plan-time refusal is the member's REFUSED task, and a template
  member whose slots fail validation — which creates no task —
  carries instantiation's refusal on the member record itself.
  A malformed submission (no members; a member naming both a
  text and a template, or neither) is refused whole as a
  `BatchError` (HTTP 422, `batch_refused`) before anything is
  stated. The record deliberately stores **no status**: the
  batch's two reads are folds over the member records, computed
  at the moment of asking. The **run sheet** joins each member
  with its task's live state and the next act that state is owed
  (`next_act_for`, one derivation shared by every read: confirm
  for PLANNED — naming the planner, whom confirmation must not
  be — execute for CONFIRMED, re-plan for STALE, resume/restart/
  abandon for EXECUTION_FAILED, corrective-or-accept for
  DISCREPANCY, nothing for a terminal state, with the ending
  named). The **report** folds counts by state, the failures
  with their steps and errors, the discrepancies still open
  with their diffs and correction chains, the refusals with
  their reasons, and the **credited total**: summed from the
  VERIFIED members' stored verification reports — the
  credit-memo `amount` field checks the re-read passed, never
  the plans' values, so a member in DISCREPANCY contributes
  nothing whatever its plan promised. A batch is complete
  exactly when every member stands in a terminal state
  (`TERMINAL_STATUSES`: VERIFIED, DISCREPANCY_ACCEPTED, REFUSED,
  REJECTED, ABANDONED — DISCREPANCY is not terminal; it still
  owes an exit), and the report names the members still owing
  acts instead of merely saying so. There is no batch lifecycle
  in the state machine below: batches add no task transitions,
  and members share nothing but the batch id (stamped on the
  task as `batch_id`) — one member's staleness, failure, or
  discrepancy cannot touch another's plan, confirmation, or
  execution, which the tests and evals prove by driving members
  of one batch to different fates on one store.
- **Audit exports** (`exports.py`, `service.py`) — the trail,
  made portable without becoming editable. An export takes one
  scope's entries in the trail's true order — a task's own
  trail; a batch's, meaning the batch scope's entries that name
  the batch plus every member task's trail, interleaved by the
  global audit read (append order in memory, the audit table's
  row id in SQL — chronological by construction on both
  backends, so the chain order is the order the work happened,
  never a per-task concatenation); or a governance scope's
  (`roles`, `templates`) — and chains them: each entry's
  SHA-256 over the previous entry's hash and its own canonical
  content (the stored fields only, keys sorted), under a
  header naming the scope, the exporter, the export time (the
  service's shared `_now()` stamp), the entry count, and the
  final hash. `verify_export` re-walks a document and reports
  intact, or the 1-based position of the first entry whose
  content, link, or index fails; a truncated tail — every
  present entry sound — is caught by the header's count and
  final hash and named at the first missing entry. The JSON
  document is the thing verified; the CSV rendering carries
  the same entries and hashes for human readers. Exporting is
  a recorded act behind the identity gate the other acts use:
  the exporter must resolve to an enrolled, active directory
  entry — an unknown name (`unknown_operator`) or a
  deactivated one (`operator_inactive`) is refused, with the
  refusal (`export_refused`) written into the exported scope's
  trail before it raises. The document is built first and the
  `audit_exported` entry (scope, count, final hash) written
  after, so a document never contains its own export entry:
  the chain covers the trail as it stood at export, and the
  next export of the scope chains the earlier act in with the
  rest. Verifying a document is a read, not an act — it writes
  nothing, because checking evidence must not write on the
  trail it checks. One route-table consequence, deliberate:
  the batch export is the only batch write beside creation.
  It decides nothing about any member — the round-8 proof
  names it as the invariant's single exception and probes
  that a member's state is untouched by it.
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
  execution claimed while one was running). The identity acts join
  them: under the `roles` scope, `operator_enrolled`,
  `operator_role_changed` (before and after), `operator_deactivated`,
  `operator_reactivated`, and `directory_refused` (the act, the
  target, the code); on the task, `task_picked_up` (from and to,
  with the planner named) and `pickup_refused`; and
  `execution_completed` / `execution_failed` now name the executor
  alongside the driver. Deactivation refusals ride the acts' own
  `*_refused` entries with code `operator_inactive`. The task
  library adds its own: under the `templates` scope,
  `template_published` (the name, the version, the version it
  supersedes), `template_refused` (the act, the target, the
  code), and `template_instantiation_refused` (the template, the
  version, the code, the reason); and on the task itself,
  `template_instantiated` (the template, the exact version used,
  the version requested, and the slot values). Batches add:
  under the `batches` scope, `batch_created` (the members as
  creation left them — task ids in submission order, and the
  refusal a member carries when no task was created) and
  `batch_refused` (a malformed submission, with its code); and
  on each member task, `batch_member` (the batch id, the
  member's position, and how it was submitted) — so a task's
  trail names the list it arrived in, and the batch's scope
  names every task it stated. Audit exports add two, into
  whichever scope was exported: `audit_exported` (the scope,
  the entry count, and the final chain hash the document
  carries — written after the document was built, so the
  document itself never contains it) and `export_refused`
  (the identity code: an exporter who is unknown to the
  directory, or deactivated).

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
  and an eval case proves it produces no plan deviation. The
  round-10 red team (`docs/red-team.md`) attacks the boundary on
  every surface — record fields, task text, bridge reports,
  template slots — graded zero-tolerance, and its plan-shape proof
  is byte-equality against a benign control. It caught one real
  hole, in the planner: a credit task's amount was read from the
  first $-figure in the text, so a figure inside the reason
  ("refund of $50 overcharge, $120") silently became the amount
  — and a stated $900 could hide behind it, under the cap. The
  amount is now the task's last figure, the position every
  governed shape states it in, and the reason is sliced up to it.
  The same discipline binds the operator's own text: a claimed
  identity or a "skip confirmation" inside a task is data to the
  grammar, never a credential or a gate state — both are in the
  pack, and both gain nothing.
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
- **An exported trail cannot be quietly rewritten.** Every
  entry in an export is chained to the one before it, and the
  header carries the count and the final hash, so the four
  quiet edits — change an entry, drop one, swap two, cut the
  tail — each break the chain at a named position. The chain
  proves the document is the trail as it stood at export; it
  does not prove the trail itself was honest, which is what
  the rest of this shell is for.
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
- **Deactivation strands work visibly; it never moves it.** A
  deactivated operator's unfinished tasks stay exactly where they
  were, under the deactivated name, until a supervisor picks one
  up as a recorded act — there is no auto-reassignment and no
  deletion, because silently moving a person's open work would
  rewrite whose work it was. And the name itself is never
  deleted, so the trail it left stays attributable: the
  attribution check resolving a deactivated name is a pass, not a
  finding.
- **Grouping is not authority.** A batch organises attention and
  takes no acts: there is no batch confirmation, batch
  execution, or batch decision route at all — the route table
  itself is the proof (batch routes are creation plus reads;
  every decision route names a task id, and the plausible bulk
  paths answer 404/405). The reason is the gate's shape: a
  confirmation binds one plan version on one task, given by one
  named second person. A "confirm all" would have to blur whose
  yes approved which values, so the shell does not offer it —
  the run sheet shows the desk exactly which member needs which
  act from whom, and each act is then taken on that task,
  recorded as that person's.

## Production profile

`docker-compose.yml` runs the app with PostgreSQL (pgvector image)
and a SeaweedFS S3 gateway (service name `minio`, port 8333) per
the house topology; the app container is multi-stage (Node stage
bundles the frontend) and runs as a non-root user. Structured JSON
logs carry a per-request id (`X-Request-ID`), and `/health` +
`/readiness` report the store profile and record counts.
