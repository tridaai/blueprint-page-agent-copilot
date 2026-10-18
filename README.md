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
  REFUSED). Executing an unconfirmed plan is refused (HTTP 409).
- **Two drivers, one step interface** — the real `page-agent`
  package in the operator's browser (live), and a deterministic
  offline driver (tests, evals, fallback), labelled honestly in
  every audit entry it produces.
- **Verification in code** — post-execution diff of record state
  against the approved plan. A wrong write is a DISCREPANCY naming
  the fields; the task is never reported successful.
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

The R1 planner is deterministic — a plan may never invent a value,
so a task it cannot derive exactly is refused as
`no_plan_derivable`, not guessed at:

| Task shape | Example |
|---|---|
| Credit memo | `raise a credit memo for invoice 4471, reason damaged goods, $120` |
| Order status | `mark order 1032 as shipped` |
| Invoice status | `mark invoice 4474 as paid` |
| Customer update | `update customer 2041 phone to 555-0134` |

Plan-time refusals, each with its reason recorded on the task:
`credit_above_cap`, `payment_field`, `delete_refused`,
`field_not_plannable`, `value_not_governed`, `record_not_found`,
`no_plan_derivable`.

## Verification and audit

After either driver runs, `verify.py` compares every planned field
against the store: SET steps against the record's current value,
CREATE steps against the created record's fields. The browser
bridge's step reports are written to the audit as reports — the
verdict comes from the diff, and an eval case proves a bridge that
reports success while doing nothing still lands in DISCREPANCY.

Every task's audit (`GET /api/tasks/{id}/audit`) carries the plan
version, the confirming operator, each step's before/after values,
the driver name, and the verification outcome.

## Layout

```
src/page_agent_copilot/
  api.py          FastAPI app: console data, task API, bridge, health
  service.py      the state machine (intake/plan/confirm/execute/verify)
  planner.py      task text + records -> field-level plan, or a refusal
  policy.py       the plan-time refusals (cap, payment fields, deletes)
  drivers.py      the step interface + the deterministic offline driver
  verify.py       post-state diff against the approved plan
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
evals/            the scripted-task gate (12 cases)
tests/            the pytest suite
docs/             architecture notes
```

## Tests and evals

```bash
make test    # 35 passed, 1 skipped (the PostgreSQL conformance leg
             # is env-gated on COPILOT_TEST_DATABASE_URL)
make evals   # 12/12 cases: happy paths diffed exactly, all three
             # plan-time refusals, the unconfirmed-execution gate,
             # a tampered write caught as DISCREPANCY, and the live
             # bridge graded on store state, not its report
```

## Honest status

- The **live in-browser path is code-complete but not yet run** in
  the build environment: it needs a real browser session and a
  provider key, and this repo's automation has neither. The console
  bridge is written against the installed package's verified API
  (constructor config and `execute()` result shape are quoted in
  `docs/architecture.md`), and the shell side of the live path —
  arming, step reports, verification — is fully covered by tests
  and evals that simulate the browser's reports. The offline driver
  is the default everywhere until a live run is on record.
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
