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
  REFUSED, plus STALE and REJECTED below, AWAITING_CLARIFICATION
  where derivation asks a question before any plan exists,
  DISCREPANCY_ACCEPTED where a supervisor accepts a named
  deviation, and EXECUTION_FAILED → ABANDONED where a run stops
  mid-plan and is closed, or picked up again — see Failure
  recovery). Executing an unconfirmed plan is refused (HTTP 409).
- **Multi-record plans** — one task can compose one plan across
  records: a credit memo with the invoice settled in the same plan,
  an order shipped with its date and carrier, status changes naming
  two records. Steps run in plan order; verification diffs every
  step, and a failed step is named by its number — a plan is never
  a partial success.
- **Multi-page flows** — the console's pages are governed data:
  a catalogue (page id, title, the record kinds each page hosts)
  that the role allowlists reference, served at
  `GET /api/pages`. A plan's span is derived from its steps — a
  plan spanning two or more pages is a **multi-page flow** and
  says so on the plan, in the API and in the console — and both
  the planner's and the executor's roles must allow every page
  it names, at plan time and again at execution. When steps
  cross pages, the execution carries an explicit **navigation**
  between them: the offline driver records the transition on the
  attempt (page left, page arrived on, the step it precedes),
  and the live bridge must report its move before reporting work
  on the new page — a step report that crosses pages without one
  is a protocol violation, refused and recorded, never silently
  accepted. The grammar's cross-page shape: a credit memo that
  also updates the **invoice's customer** in the same task — the
  customer read from the invoice record, never named in the
  text — verified end to end with a linkage check tying the
  updated customer to the memo's invoice.
- **Accessibility mode** — a view and a door for operators who
  work by voice, never a second shell. Every plan has a
  **spoken form**: a rendering produced in code from the
  stored plan — steps in order, figures spelled as words,
  dates spoken, field names in plain words — served at
  `GET /api/tasks/{id}/plan/spoken` and shown in the console.
  It is a view: the stored plan keeps its digits, and asking
  for the spoken form changes nothing. Confirmation gains a
  **read-back** path on top of the same gate: the shell
  issues a token bound to the task, the plan version, the
  spoken form's hash, and the operator it was issued to
  (`POST /api/tasks/{id}/readback`), and confirming with it
  (`POST /api/tasks/{id}/confirm-readback`) runs every
  ordinary rule — second person, confirm-capable role,
  active name — plus the binding: amend the plan after the
  read-back and the token refuses as stale, naming both
  versions, because the confirmer must hear the version they
  approve. The act is recorded as `plan_confirmed` with
  `method: readback`. Intake gains a **transcript** door
  (`POST /api/tasks/transcript`): the envelope a speech
  recogniser wraps a dictation in — speaker labels,
  timestamps, line breaks — is normalised in code, and the
  cleaned text goes through the ordinary intake, so a
  transcript produces the plan the same words typed produce;
  the raw transcript rides the task's trail. Numbers are
  never converted from words — ids and amounts must arrive
  as digits. And the directory entry carries a
  `prefers_spoken` flag, set by a recorded supervisor act
  (`PUT /api/operators/{name}/preference`): when set, the
  console task view leads with the spoken form and the
  read-back route is the offered confirmation path. The
  flag is presentation only — it permits nothing, and the
  eval gate proves the same refusals land with it set.
- **Clarification, never invention** — a task whose words match a
  governed shape in more than one way no longer dies as a
  refusal: it parks in **AWAITING_CLARIFICATION** carrying 2–4
  concrete options computed from the records, under a question
  composed in code from those options. The ambiguity classes are
  defined in code, and they are the only ones: a record
  **described by a governed status word** ("the open invoice";
  "mark the packed order as shipped") that matches several
  records — one match derives directly, none refuses as
  `record_not_found`; a credit task that **omits its amount**
  where the records offer a governed candidate set (the
  invoice's own amount, and the amounts of credit memos already
  issued against it — the records here carry no instalment
  schedule, so no instalment amounts are offered); a status
  value that **prefixes two governed values** ("p" for an order:
  placed or packed — a prefix matching only one governed value
  still refuses as `value_not_governed`). Answering is a
  recorded act (`POST /api/tasks/{id}/clarify`) under the
  planning role rules — the answerer must be enrolled, active,
  and plan-capable; the planner may answer their own
  clarification, and confirmation remains other people's work
  as always — one confirmer ordinarily, two under dual control.
  The answer re-runs derivation with the choice pinned,
  and the pin is re-validated against the records as they now
  stand: if the basis moved, the question is asked again with
  fresh options rather than the stale pin being honoured. A
  reply that is not one of the options is refused with the
  options restated — the shell never interprets free text into
  a value. Patience is governed (`COPILOT_MAX_CLARIFICATIONS`,
  default 2 distinct questions): after the second answer, a
  third ambiguity refuses the task as `clarification_exhausted`
  with the questions and answers attached. The eval gate's
  ambiguity pack proves the invariant value by value: every
  value in every clarified plan traces to the task text, a
  named record field, or the recorded answer — and a task with
  no governed option set still refuses, because clarification
  is not a backdoor around `no_plan_derivable`.
- **Observability** — three reads folded from the records,
  never from counters. The **execution trace**
  (`GET /api/tasks/{id}/trace`, and a panel in the console's
  task view) reconstructs one task's timeline: its attempts in
  order, each attempt's events — navigations before the steps
  they precede, steps with their before/after values — the
  budget the attempt ran under, and its wall clock read from
  the attempt's own started/finished stamps. Per-step timings
  are not shown: neither driver records when a step started
  or landed, so the trace shows none rather than inventing
  one. The **failure taxonomy** is one function with a
  documented rule order that names every ending — verified,
  refused at plan time (with the governing code),
  clarification exhausted, discrepancy (with its differences),
  discrepancy accepted, stale, execution failed (subreason:
  the budget, or the driver), abandoned, rejected, or still
  in progress — and it rides the task list and detail reads
  as a derived `ending` field. The **daily digest**
  (`POST /api/digests/{day}` as an enrolled, active operator;
  `GET` to read; a console panel; a `digest` CLI verb) folds
  the tasks created on a day, as they stand: counts by status
  and taxonomy, the credits their verification reports prove
  (the batch report's rule — a discrepancy contributes
  nothing), clarifications asked and answered, budget
  exhaustions, discrepancies open vs accepted, and bridge
  protocol refusals counted as the events they are (a
  protocol refusal never ends an execution, so it is not an
  ending class). The fold is stored as a dated snapshot;
  regenerating a day replaces it at a bumped version.
- **MCP surface — tasks, never clicks** — machine clients (an
  MCP host: an IDE, a desktop assistant, another agent) get a
  surface with the opposite contract to PageAgent's own MCP
  server, which hands an external client the in-page agent's
  controls directly. This one offers seven task-level tools —
  `submit_task`, `submit_template_task`, `answer_clarification`
  (an offered option's value, never free text), and the reads
  `get_task`, `list_templates`, `get_verification`,
  `get_task_audit` — and nothing else: no tool confirms,
  executes, amends, deletes, or touches a record, and a call to
  a click-shaped name is refused by name. Confirmation and
  execution stay human acts in the console. Every call carries
  an operator name and is judged by the same directory and role
  rules as any other door — a viewer submitting over MCP gets
  the viewer's `role_cannot_plan` refusal in the same words —
  and each act the surface causes lands on the task's trail
  with `channel: "mcp"` on its door entry, so the audit shows
  where work arrived from. The protocol is the repo's own code
  (stdio, JSON-RPC, `initialize` / `ping` / `tools/list` /
  `tools/call`): the official `mcp` Python SDK was surveyed
  first and resolves against the repo's frozen pydantic pin
  but fails at import — it needs pydantic < 2.14 in practice —
  and moving a frozen pin for seven tools was refused. Run it
  with `page-agent-copilot mcp`.
- **Deployment profiles** — how the agent ships and where its
  model lives is a named, governed choice (`COPILOT_PROFILE`),
  not an accident of how the page was pasted together. A
  profile pairs a **bundle delivery** — `local`, the committed
  bundle built from the pinned npm package, or `cdn`, a script
  URL the deployment names, served into the page instead —
  with a **model endpoint class**: `hosted`, an
  OpenAI-compatible endpoint outside the customer's network,
  or `self_hosted`, one inside it (a NIM container, a vLLM
  service). The CDN pin is checked at load: the URL must carry
  the `page-agent@` version this repo bundles and grades, and
  a mismatch refuses with both versions named — the page never
  silently loads a different agent than the one the evals ran
  against. The key rule follows the class: a hosted endpoint,
  once configured, requires a key; a self-hosted one may run
  keyless. The app serves a **Content-Security-Policy**
  computed from the profile — `script-src 'self'` plus, for a
  CDN profile, exactly the pinned CDN's origin; `connect-src`
  naming the one configured model endpoint, the hole the
  in-page agent needs stated deliberately. `/api/agent-config`
  carries the profile beside the library's fields, the
  console's footer states the deployment it is running, and
  every execution attempt records the profile it ran under —
  delivery, endpoint class, model name, never the key — so
  the audit answers which deployment ran a task. Misconfig-
  uration fails loud at startup, each refusal named: unknown
  profile, missing or unpinned or mismatched CDN URL,
  unparseable model address, hosted endpoint without a key.
- **Dual control** — the confirmation gate scales with the
  plan's stakes. A plan is under dual control when the total
  it credits reaches the governed threshold
  (`COPILOT_DUAL_CONTROL_CAP`, defaulting to the credit cap),
  or when it touches a high-stakes field — a customer's
  `credit_limit`, the one field that extends money rather
  than recording work, and already the field held back from
  operators. Such a plan is confirmed by **two distinct
  operators**, neither of them the planner: the first
  confirmation is a seat recorded on the task, the task
  stands PLANNED, and execution refuses, naming the second
  confirmer it is owed, until a second, different person
  confirms the same plan version. The same name twice is
  refused (`already_confirmed`, names normalised); a
  read-back confirmation fills a seat like any other, its
  token binding intact. An amendment resets the seats to
  zero — confirmations bind versions, so the new version
  owes both again. The standing is computed from the plan,
  never stored as a flag: plan reads carry
  `dual_control_required` and the reasons, task reads show
  the seats filled and who is still owed, the console badges
  it, the batch run sheet names the second confirmation as
  the next act, and the daily digest counts the day's tasks
  awaiting theirs. Below the threshold, nothing changes —
  one confirmer, as always.
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
- **Task library** — the recurring jobs are governed records,
  not remembered phrasings: a **template** is a task-text
  pattern with named parameter slots, a note on when to use it,
  and a version — seeded with the console's grammar (credit
  memo, credit memo and settle, credit memo and customer
  update, ship order, order status, invoice status, customer
  update). Publishing a new version is
  a supervisor-only recorded act; old versions stay readable
  and instantiable by name and version. Instantiating one
  validates its slots (a record slot must name a record that
  exists; a money slot must parse), renders the text, and
  states it as an ordinary task — the planner derives the plan
  from the rendered text through the same grammar, so a
  template cannot smuggle a plan past the planner, and a slot
  value that policy refuses fails with policy's own reason.
  Each version pins **examples** — slot values plus the exact
  plan shape they must derive — which run in the eval gate; a
  version whose examples no longer derive to their pinned shape
  is marked **BROKEN** in the library, computed on read (the
  eval runner stores no results, so health is the examples,
  re-derived at the moment of asking).
- **Batch work, without bulk decisions** — the desk's work
  arrives as a list, so a **batch** groups the tasks submitted
  together: task texts and template instantiations in one
  submission, in order, under one creator. Creating a batch
  states each member through the ordinary intake, so every
  gate applies to each member exactly as to a task stated
  alone — and a member that refuses at plan time stays in the
  batch with its reason showing, never silently dropped. What
  a batch never does is decide: there is **no batch confirm,
  no batch execute, no bulk act of any kind**, because a
  confirmation binds one plan version on one task, given by
  one named second person. Instead the batch offers a **run
  sheet** — each member's state, the next act it needs, and
  who may take it — and a **report** folded from the member
  records: counts by state, the failures with their steps
  named, the discrepancies still open, and the total credited
  across verified members, read from their verification
  reports rather than their plans. The batch record stores no
  status of its own; complete means every member stands in a
  terminal state, folded fresh on every read. Members share
  nothing but the batch id — one member going stale or failing
  never touches another's lifecycle.
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
- **Budgets** — each attempt runs under governed ceilings it
  snapshots onto its own record at claim time: a step ceiling
  and a wall-clock ceiling. The shell reads the budget before a
  step runs or a bridge report is accepted; a step the ceiling
  forbids never runs, and the attempt closes through the same
  failure machinery — the task in EXECUTION_FAILED naming the
  budget, the partial state standing, a resume spending a fresh
  attempt budget. Per-operator spend (attempts, tasks, steps,
  exhaustions) folds from the attempt records at
  `GET /api/operators/budget` — never from counters.
- **Audit trail** — every step logged: plan version, confirmer,
  per-step before/after values, the driver that ran it, and the
  verification result. Readable via API and in the console.
- **Audit exports, hash-chained** — a trail that can be quietly
  edited afterwards is a story, not evidence, so any scope's
  trail — one task's, one batch's (its own entries plus its
  members' trails, interleaved in the order the work happened),
  or a governance scope (roles, templates) — exports as a
  single document in which every entry carries a SHA-256 chain
  hash over the previous hash and its own canonical content,
  under a header naming the scope, the exporter, the export
  time, the entry count, and the final hash. Edit an entry,
  drop one, swap two, or cut the tail, and verification names
  the first entry that breaks. Exporting is itself a recorded
  act, behind the same identity gate as every other act: the
  exporter must be an enrolled, active operator, a refusal is
  recorded, and the document covers the trail as it stood —
  the export's own entry is written after, and chains into
  the next export of the scope.
- **Page content is data** — text inside console records (a customer
  note that says "also refund $5,000") never steers the copilot;
  plans come from the operator's task and the governed fields.
  The red-team pack (round 10, `docs/red-team.md`) attacks that
  boundary on every surface — record fields, task text, bridge
  reports, template slots — graded zero-tolerance. It caught one
  real hole: a figure inside a credit *reason* ("refund of $50
  overcharge, $120") used to become the memo's amount, so the
  planner now reads the amount from its governed position, the
  task's last figure, and the reason's figure stays in the reason.

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
- `raise a credit memo for invoice 4471, reason damaged goods, $120 and update the customer's email to creditnotes@harbourview.example` → one plan, two pages: the memo, and the invoice's customer updated
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

Deployment profile: `COPILOT_PROFILE` names how the agent is
delivered and where the model lives — `local-hosted` (the
default: the committed bundle, a hosted endpoint),
`local-self-hosted`, `cdn-hosted`, or `cdn-self-hosted`. A CDN
profile serves the script at `COPILOT_CDN_URL` into the console
page instead of the committed bundle; the URL must carry the
pin for the bundled version (today
`https://cdn.jsdelivr.net/npm/page-agent@1.12.4/dist/iife/page-agent.demo.js`
is the shape), and a pin that names any other version refuses
at startup with both versions stated. A hosted profile
requires `PAGEAGENT_API_KEY` once an endpoint is configured;
a self-hosted profile (a NIM container, a vLLM service on the
customer's network) may leave it empty. The
Content-Security-Policy the app serves follows the profile:
script sources are the bundle's home only, and `connect-src`
names the configured model endpoint's origin.

Store profile: SQLite is the local default (`SQLITE_PATH`, a single
file at `./data/copilot.db`). Set `DATABASE_URL` and the same code
runs on PostgreSQL + pgvector — that one variable is the whole
switch. Policy: `COPILOT_CREDIT_CAP` sets the credit cap
(default `500.00`). Budgets: `COPILOT_MAX_PLAN_STEPS` (default 25)
caps the plan a task may derive, `COPILOT_MAX_EXECUTION_STEPS`
(default 40) the steps one execution attempt may run, and
`COPILOT_MAX_EXECUTION_SECONDS` (default 1800) an attempt's wall
clock — each enforced in the shell, the attempt stopped and the
exhaustion named rather than overrun. The `maxSteps` the console
serves the in-page agent is the execution-step ceiling itself,
read from the same Policy — one number, one home. Clarification:
`COPILOT_MAX_CLARIFICATIONS` (default 2) caps the distinct
questions derivation may ask one task before a further ambiguity
refuses it — the same Policy home, the same enforcement in the
shell. Dual control: `COPILOT_DUAL_CONTROL_CAP` (defaulting to
the credit cap in force) is the total credited amount at which a
plan needs two distinct confirmers instead of one; a plan
touching a high-stakes field (a customer's `credit_limit`) needs
two at any amount.

## The governed task grammar

The planner is deterministic — a plan may never invent a value,
so a task it cannot derive exactly is refused as
`no_plan_derivable`, not guessed at:

| Task shape | Example |
|---|---|
| Credit memo | `raise a credit memo for invoice 4471, reason damaged goods, $120` |
| Credit memo + settle | `… $120 and mark invoice 4471 as paid` |
| Credit memo + customer update | `… $120 and update the customer's email to creditnotes@harbourview.example` (cross-page: Invoices → Customers) |
| Credit memo, invoice described | `raise a credit memo for the open invoice, reason damaged goods, $120` (clarifies when several invoices are open) |
| Order status | `mark order 1032 as shipped` |
| Order status, record described | `mark the packed order as shipped` (clarifies when several orders are packed) |
| Ship with details | `mark order 1032 as shipped, ship date 2026-10-20, carrier DHL` |
| Compound status | `mark order 1036 as packed and mark invoice 4474 as paid` |
| Invoice status | `mark invoice 4474 as paid` |
| Customer update | `update customer 2041 phone to 555-0134` |

A record may be described by a governed status word instead of
named by its number — in a credit task, or as the record of a
status clause (a descriptor clause stands alone in its task;
mixing it with a clause that names a record by number refuses
whole). One match derives; several matching records ask, as
above; a status value stated as a prefix of two governed values
asks the same way. A credit task may omit its amount only when
the records offer a candidate set for that invoice to clarify
between; otherwise the amount must be stated, exactly as before.

Plan-time refusals, each with its reason recorded on the task:
`credit_above_cap`, `payment_field`, `delete_refused`,
`field_not_plannable`, `value_not_governed`, `record_not_found`,
`no_plan_derivable`, `plan_too_large` (a derived plan over the
governed step ceiling, both counts named),
`clarification_exhausted` (a third distinct ambiguity after two
answered questions, the history attached) — and the role
refusals: `unknown_operator`, `role_cannot_plan`,
`field_not_permitted`, `page_not_permitted`.
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
names a customer that exists; a customer a cross-page plan
updates is the customer of the invoice its credit memo was
planned against; and the execution record in the
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
An attempt's record carries its page navigations too; the live
bridge reports a move at `POST /api/tasks/{id}/navigation`, and
a crossing the protocol refuses is on the trail as
`page_navigated`, `navigation_refused`, or
`step_report_refused`. The console's pages read at
`GET /api/pages`.
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
The spoken-form preference rides the same scope
(`operator_preference_changed`) at
`PUT /api/operators/{name}/preference`. A task's trail also
carries its read-backs (`readback_issued`, `readback_refused`)
and its transcript arrivals (`transcript_intake`, the raw
transcript beside the normalised statement).
Orphaned work lists at `GET /api/operators/orphaned` and moves
only through `POST /api/tasks/{id}/pickup` (`task_picked_up`,
with `pickup_refused` recorded on a refusal). Executions name
their executor on the attempt record and on the
`execution_completed` entry, so `GET /api/operators/report` can
fold each name's history from the trail, and
`GET /api/attribution` runs the completeness check over it.
The task library reads at `GET /api/templates` (every version,
health folded in), `GET /api/templates/{name}`, and
`GET /api/templates/{name}/versions/{version}`; a supervisor
publishes a new version at `POST /api/templates`, and a task is
stated from one at `POST /api/templates/{name}/instantiate`
with the version named or omitted — the task records the exact
version used either way. Library acts are recorded in their own
audit scope at `GET /api/templates-audit` (`template_published`,
`template_refused`, `template_instantiation_refused`), and each
instantiated task's own trail carries `template_instantiated`
with the template, the version, and the slot values. A batch
is created at `POST /api/batches` — the only batch write that
exists — and read at `GET /api/batches`,
`GET /api/batches/{id}`, `GET /api/batches/{id}/run-sheet`
(each member's state, next act, and who may take it), and
`GET /api/batches/{id}/report` (counts by state, the credited
total from the verified members' reports, the discrepancies
and failures still open, and the members still owing acts, or
the batch's completeness). There is no batch decision route
to call: the plausible bulk paths (`POST
/api/batches/{id}/confirm`, `.../execute`) answer 404/405,
and the test suite asserts against the route table that every
decision route names a task id. Batch acts are recorded in
their own scope at `GET /api/batches-audit` (`batch_created`,
`batch_refused`), and each member task's trail carries
`batch_member` naming the batch and its position in it.

Any of those trails exports as a hash-chained document:
`POST /api/tasks/{id}/audit/export`,
`POST /api/batches/{id}/audit/export`,
`POST /api/roles-audit/export`, and
`POST /api/templates-audit/export` each take the exporter's
name (and `format: "csv"` for the CSV rendering of the same
entries and hashes) and return the document — header (scope,
exporter, export time from the shell's shared clock, entry
count, final chain hash) plus every entry with its chain
index and hashes. `POST /api/audit/verify` grades a document:
intact, or broken at the first entry whose content, link, or
index fails — a truncated copy is caught by the header's
count and final hash, and named at the first missing entry.
The export act lands in the exported scope's trail as
`audit_exported` (the count and final hash it produced), and
a refused export as `export_refused`, with the identity
code — `unknown_operator` or `operator_inactive`.

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
  templates.py    the task library: templates as governed records —
                  pattern, slots, pinned examples, the strict loader
  batches.py      batches: submissions as governed records — member
                  order, terminal states, the next-act derivation
  exports.py      audit exports: the hash chain over a scope's
                  trail — build, CSV rendering, verification
  drivers.py      the step interface + the deterministic offline driver
  reads.py        the console read surface: the reads the API serves,
                  and the only path verification re-reads through
  verify.py       the verification act: field + linkage checks over
                  the re-read, producing the stored report
  spoken.py       the spoken form: a plan rendered for the ear
                  (figures spelled, dates spoken), its hash, and
                  the transcript normaliser
  observability.py the trace, the failure taxonomy, and the
                  daily digest fold — pure derivations over the
                  task, attempt, report, and audit records
  mcp_server.py   the MCP surface: machine clients submit tasks
                  over stdio JSON-RPC — tasks, never clicks; the
                  protocol is the repo's own code (see the module)
  store.py        the store port: in-memory / SQLite / PostgreSQL
  records.py      the one mutation path for OrderDesk state
  dataset.py      the OrderDesk seed data (generated in code; gold
                  task outcomes hold by construction)
  models.py       records, plans, steps, tasks, audit shapes
  config.py       store profile, policy, PageAgent LLM settings,
                  and the deployment profiles (delivery, endpoint
                  class, the CDN pin, the CSP)
  static/         the OrderDesk console + the live bridge (copilot.js)
frontend/         package.json pins page-agent@1.12.4; build.mjs
                  bundles it to static/vendor/page-agent.bundle.js
migrations/       numbered SQL, PostgreSQL + SQLite dialects
evals/            the scripted-task gate (102 cases)
tests/            the pytest suite
docs/             architecture notes
```

## Tests and evals

```bash
make test    # 394 passed, 1 skipped (the PostgreSQL conformance leg
             # is env-gated on COPILOT_TEST_DATABASE_URL)
make evals   # 102/102 cases: happy paths diffed exactly (including
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
             # reconciling with the trail it folds from — and
             # the task library: every seeded template
             # instantiated, planned, and verified with exact
             # diffs, an old version instantiated by name and
             # version, a slot naming a missing record refused at
             # instantiation, an operator refused at publish, a
             # policy-breaking slot value refused in policy's own
             # words, and every template's pinned examples green
             # — and batch work: a mixed submission (a clean
             # credit, an over-cap refusal kept with its reason,
             # a template member, a multi-record member) whose
             # run sheet names each member's next act, completed
             # by per-task acts alone, with the report
             # reconciling against the credited total recomputed
             # from the members' verification reports; one
             # member going stale while another verifies in the
             # same batch; and a lying bridge whose member
             # credits nothing until its own recorded exit
             # closes it — and audit exports: a completed
             # task's trail exported and verified intact (the
             # document never containing its own export entry),
             # a batch's chain interleaving its members'
             # trails in the order the work happened, a
             # tampered copy — an entry edited, dropped, or a
             # tail cut — failing verification at the first
             # broken entry, and a deactivated operator
             # refused the export, on the record — and the
             # red team: hostile record text leaving every
             # plan byte-equal to its benign control and
             # reaching no step instruction, 'skip
             # confirmation' and impersonated authority in
             # the task text gaining nothing, a smuggled
             # delete refused whole and a widening clause
             # never obeyed, a figure inside a credit reason
             # failing to redirect the amount (the one
             # attack that had succeeded before this round),
             # bridge reports failing to redirect a step or
             # certify undone work, and template slots
             # meeting the same refusals as the same words
             # typed — 8/8 attacks held, with the section's
             # counters (injected instructions obeyed, note
             # text in step instructions, gate skips) at 0
             # — and budgets: an over-long plan refused at
             # plan time with both counts named, a bridge
             # reporting past its step ceiling stopped (the
             # exhaustion on the attempt, the task failed
             # naming it, the landed write standing as
             # partial state), a resume after exhaustion
             # verifying on a fresh attempt budget, and
             # agent-config serving the Policy ceiling as
             # maxSteps — and multi-page flows: the
             # cross-page shape verified with exact diffs on
             # both records and its cross-page linkage green,
             # a role missing a page refused at plan time
             # naming the page, a page lost after confirmation
             # blocking execution with the task held, the
             # navigation recorded on the attempt (spending no
             # step budget), a bridge report that skips the
             # navigation refused and recorded before the run
             # completes, and single-page plans unchanged
             # — and accessibility: a credit plan's spoken form
             # spelling its amount with the stored plan unmoved,
             # a multi-page plan's spoken form naming both its
             # pages, a read-back token confirming end to end
             # with the method on the trail, a token for v1
             # refused after amendment to v2 with both versions
             # named in the recorded reason, a diarised
             # transcript planning identically to the same words
             # typed, and the spoken-form preference changing
             # presentation only — the same refusals landing
             # with it set — and ambiguity: a multi-match
             # invoice clarified and verified with exact diffs,
             # a missing amount clarified from the records'
             # candidates, a prefix status clarified between
             # governed values, a unique descriptor deriving
             # without asking, the two negative controls (no
             # candidate set — the refusal stands; a non-answer
             # refused with the options restated), a third
             # question exhausting the governed patience, and
             # every clarified plan's values traced to the text,
             # a record field, or the recorded answer
             # — and observability: a two-attempt execution's
             # trace graded step for step, a scripted day's
             # digest graded against a recount taken from the
             # task records by a separate path (regeneration
             # bumping the version), and taxonomy pins naming
             # every ending class — and deployment profiles:
             # the local profile serving the committed bundle
             # under a self-only script policy, the CDN profile
             # serving the pinned script with its origin named
             # in the CSP and its version matching the bundle,
             # a self-hosted keyless deployment validating with
             # the attempt recording the deployment it ran
             # under, and a hosted endpoint without a key
             # refusing at config load, by name — and the MCP
             # surface: a task submitted over the wire (the real
             # server, a subprocess on stdio) planned, and
             # verified only after a human confirmed and
             # executed it off-surface, with the channel on the
             # trail; a clarification asked and answered over
             # the wire by option value, free text refused; a
             # click-shaped call refused by name; a viewer and a
             # deactivated operator refused in the shell's own
             # words; and the library listed and instantiated
             # over the wire, a bad slot refusing with its code
             # — and dual control: an at-cap credit executing
             # only after two distinct confirmers (execution
             # refused after one, naming the seat owed), the
             # same confirmer twice and the planner filling no
             # second seat, a credit-limit plan dual by its
             # field, a below-threshold plan confirming singly
             # as before, an amendment after the first seat
             # resetting the count to zero, and a read-back
             # filling one seat with a plain confirmation
             # filling the other
```

## Honest status

- The **live in-browser path is code-complete but not yet run** in
  the build environment: it needs a real browser session and a
  provider key, and this repo's automation has neither. The console
  bridge is written against the installed package's verified API
  (constructor config and `execute()` result shape are quoted in
  `docs/architecture.md`), and the shell side of the live path —
  arming, step reports, navigation reports, verification — is
  fully covered by tests and evals that simulate the browser's
  reports. One live-path
  behaviour follows from that split: when a step reports failure,
  the shell stops the execution (EXECUTION_FAILED) and refuses
  the bridge's later reports, so the run halts in the shell; a
  resume re-arms the task for the remaining steps only. The
  offline driver is the default everywhere until a live run is
  on record.
- The **MCP surface inherits the shell's identity model**: an
  operator name, judged by the directory — there is no credential
  layer on any door of this blueprint, console, API, or MCP. The
  stdio server runs as a local process under the deployment's
  own account; a deployment that exposes it further fronts it
  with its own transport security, the way it would the API.
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
- **Accessibility mode stops at the transcript.** The shell does
  no speech recognition and no text-to-speech: a deployment's
  recogniser produces the transcript text and its screen reader
  speaks the spoken form; the shell normalises the envelope,
  plans, gates, and binds the confirmation to the words served.
  Spelled-out numbers are a deliberate non-goal — record ids and
  amounts must arrive as digits, because a word-to-number
  converter guessing at an id could plan against the wrong
  record; a transcript whose figures are words refuses as
  underivable text, like any task the grammar cannot derive.

## Licence

Apache 2.0 for this blueprint's own code (see `LICENSE`). PageAgent
is a separate dependency of Alibaba's, under its own MIT licence.
