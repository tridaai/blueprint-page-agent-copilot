"""The HTTP surface: the OrderDesk console, the governed task API,
and the browser bridge endpoints.

One rule holds across all of it: state changes go through the
service / record mutations, and gate refusals surface as HTTP 409 —
the API never widens what the shell allows. Policy refusals on an
otherwise legal act (an amendment whose value breaks policy)
surface as HTTP 422 with the refusal code, and change nothing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .batches import BatchCreateIn, BatchError
from .config import Settings
from .logging_setup import new_request_id, setup_logging
from .models import (
    PAYMENT_FIELDS,
    AbandonIn,
    AcceptDeviationIn,
    AmendIn,
    AuditExportIn,
    AssignRoleIn,
    ConfirmIn,
    ConsoleSaveIn,
    CorrectiveIn,
    DirectoryActIn,
    EnrollOperatorIn,
    ExecuteIn,
    PickupIn,
    RejectIn,
    ReplanIn,
    RestartIn,
    ResumeIn,
    RoleUpdateIn,
    StepReportIn,
    TaskIntake,
    VerifyIn,
)
from .policy import PlanRefusal
from .reads import ConsoleReads
from .records import apply_field_change, create_credit_memo
from .roles import RoleError
from .service import CopilotService, GateError
from .store import Store, build_store
from .templates import (
    TemplateError,
    TemplateInstantiateIn,
    TemplatePublishIn,
)

log = logging.getLogger("page_agent_copilot.api")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(settings: Optional[Settings] = None,
               store: Optional[Store] = None,
               seed: bool = False) -> FastAPI:
    setup_logging()
    settings = settings or Settings.from_env()
    store = store or build_store(settings)
    reads = ConsoleReads(store)
    service = CopilotService(settings, store)
    if seed:
        service.seed()

    app = FastAPI(title="Page Agent Admin Copilot — OrderDesk")
    app.state.service = service
    app.state.settings = settings

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = new_request_id()
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        log.info("%s %s -> %s", request.method, request.url.path,
                 response.status_code)
        return response

    @app.exception_handler(GateError)
    async def gate_error(_: Request, exc: GateError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(PlanRefusal)
    async def plan_refusal(_: Request, exc: PlanRefusal):
        # A value policy refuses (an over-cap amendment, an
        # ungoverned status): the request was well-formed and the
        # state machine allowed the attempt, but policy says no.
        # Nothing was changed.
        return JSONResponse(status_code=422, content={
            "detail": exc.message, "code": exc.code})

    @app.exception_handler(RoleError)
    async def role_error(_: Request, exc: RoleError):
        # A role definition that does not validate (or names no
        # role): refused whole, nothing stored.
        return JSONResponse(status_code=422, content={
            "detail": exc.message, "code": exc.code})

    @app.exception_handler(BatchError)
    async def batch_error(_: Request, exc: BatchError):
        # A batch submission that does not validate (no members,
        # or a member naming both a text and a template, or
        # neither): refused whole, nothing stated, nothing stored.
        return JSONResponse(status_code=422, content={
            "detail": exc.message, "code": exc.code})

    @app.exception_handler(TemplateError)
    async def template_error(_: Request, exc: TemplateError):
        # A template that does not validate, or slot values that
        # do not fit it: refused whole, nothing stored, nothing
        # instantiated.
        return JSONResponse(status_code=422, content={
            "detail": exc.message, "code": exc.code})

    @app.exception_handler(KeyError)
    async def not_found(_: Request, exc: KeyError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    # -- health -------------------------------------------------------
    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/readiness")
    def readiness():
        customers = store.list_records("customers")
        if not customers:
            return JSONResponse(status_code=503, content={
                "status": "not seeded", "store": settings.store_kind})
        return {
            "status": "ready",
            "store": settings.store_kind,
            "records": {
                kind: len(store.list_records(kind))
                for kind in ("customers", "invoices", "orders",
                             "credit_memos")
            },
            "page_agent_configured": settings.page_agent.configured,
            "default_driver": "offline",
        }

    @app.post("/api/seed")
    def seed_now():
        seeded = service.seed()
        return {"seeded": seeded}

    # -- OrderDesk console data ----------------------------------------
    # Reads are served through the console read surface (reads.py)
    # — the same functions the verifier re-reads through, so what a
    # human sees and what verification diffs are the same shape.
    # Saves stay on the mutation path in records.py.
    def _save(kind: str, record_id: str, body: ConsoleSaveIn):
        for field in body.fields:
            if field in PAYMENT_FIELDS:
                return JSONResponse(status_code=422, content={
                    "detail": f"'{field}' is a payment field; payment "
                              f"fields are read-only in OrderDesk"})
        record = store.get_record(kind, record_id)
        if record is None:
            raise KeyError(f"{kind} record {record_id} does not exist")
        for field, value in body.fields.items():
            if field in record:
                apply_field_change(store, kind, record_id, field, value)
        return store.get_record(kind, record_id)

    @app.get("/api/console/customers")
    def customers():
        return reads.list("customers")

    @app.get("/api/console/customers/{record_id}")
    def customer(record_id: str):
        record = reads.get("customers", record_id)
        if record is None:
            raise KeyError(f"customers record {record_id} does not exist")
        return record

    @app.put("/api/console/customers/{record_id}")
    def save_customer(record_id: str, body: ConsoleSaveIn):
        return _save("customers", record_id, body)

    @app.get("/api/console/invoices")
    def invoices():
        return reads.list("invoices")

    @app.get("/api/console/invoices/{record_id}")
    def invoice(record_id: str):
        record = reads.get("invoices", record_id)
        if record is None:
            raise KeyError(f"invoices record {record_id} does not exist")
        return record

    @app.put("/api/console/invoices/{record_id}")
    def save_invoice(record_id: str, body: ConsoleSaveIn):
        return _save("invoices", record_id, body)

    @app.get("/api/console/orders")
    def orders():
        return reads.list("orders")

    @app.get("/api/console/orders/{record_id}")
    def order(record_id: str):
        record = reads.get("orders", record_id)
        if record is None:
            raise KeyError(f"orders record {record_id} does not exist")
        return record

    @app.put("/api/console/orders/{record_id}")
    def save_order(record_id: str, body: ConsoleSaveIn):
        return _save("orders", record_id, body)

    @app.get("/api/console/credit-memos")
    def credit_memos():
        return reads.list("credit_memos")

    @app.post("/api/console/credit-memos")
    def issue_credit_memo(body: ConsoleSaveIn):
        """The credit memo form's submit — used by a human at the
        console and by the in-page agent in the live path alike."""
        fields = body.fields
        invoice = store.get_record("invoices", fields.get("invoice_id", ""))
        if invoice is None:
            return JSONResponse(status_code=422, content={
                "detail": "invoice_id does not name an invoice"})
        return create_credit_memo(store, fields, task_id=body.task_id)

    # -- governed tasks ---------------------------------------------------
    @app.post("/api/tasks", status_code=201)
    def intake(body: TaskIntake):
        task = service.intake(body.text, body.operator)
        return {"task": task.model_dump(mode="json"),
                "plan": (service.get_plan(task.task_id).model_dump(mode="json")
                         if task.plan_version else None)}

    @app.get("/api/tasks")
    def tasks():
        return [t.model_dump(mode="json") for t in service.list_tasks()]

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: str):
        task = service.get_task(task_id)
        if task is None:
            raise KeyError(f"task {task_id} does not exist")
        plan = service.get_plan(task_id)
        report = service.get_verification(task_id)
        return {"task": task.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json") if plan else None,
                "plan_versions": [p.version
                                  for p in service.list_plans(task_id)],
                "attempts": [a.model_dump(mode="json")
                             for a in service.get_attempts(task_id)],
                "verification": (report.model_dump(mode="json")
                                 if report else None)}

    @app.get("/api/tasks/{task_id}/verification")
    def verification(task_id: str):
        if service.get_task(task_id) is None:
            raise KeyError(f"task {task_id} does not exist")
        report = service.get_verification(task_id)
        if report is None:
            raise KeyError(
                f"task {task_id} has no verification report")
        return report.model_dump(mode="json")

    @app.post("/api/tasks/{task_id}/verify")
    def verify(task_id: str, body: VerifyIn):
        # Verification is an act with a record, not a
        # recomputation: it already ran at the end of execution,
        # so this is refused (409) and the refusal is recorded.
        report = service.verify_task(task_id, body.operator)
        return report.model_dump(mode="json")

    @app.post("/api/tasks/{task_id}/corrective", status_code=201)
    def corrective(task_id: str, body: CorrectiveIn):
        # Exit one from a DISCREPANCY: a linked corrective task
        # whose plan closes the named diffs. It is a task like any
        # other from here — confirm, execute, verify — and the
        # parent stays DISCREPANCY with its report untouched.
        child = service.spawn_corrective(task_id, body.operator)
        plan = service.get_plan(child.task_id)
        return {"task": child.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json") if plan else None}

    @app.post("/api/tasks/{task_id}/accept-deviation")
    def accept_deviation(task_id: str, body: AcceptDeviationIn):
        # Exit two: a supervisor accepts the named deviation, with
        # a mandatory reason. The task closes DISCREPANCY_ACCEPTED
        # — never VERIFIED — and the refusal paths (not a
        # supervisor, no reason, wrong state) are 409s.
        task = service.accept_deviation(
            task_id, body.operator, body.reason)
        return task.model_dump(mode="json")

    @app.get("/api/tasks/{task_id}/plans")
    def plans(task_id: str):
        if service.get_task(task_id) is None:
            raise KeyError(f"task {task_id} does not exist")
        return [p.model_dump(mode="json")
                for p in service.list_plans(task_id)]

    @app.get("/api/tasks/{task_id}/plans/{version}")
    def plan_version(task_id: str, version: int):
        plan = service.get_plan(task_id, version)
        if plan is None:
            raise KeyError(
                f"task {task_id} has no plan version {version}")
        return plan.model_dump(mode="json")

    @app.get("/api/tasks/{task_id}/plan-diff")
    def plan_diff(task_id: str, from_version: int, to_version: int):
        if service.get_task(task_id) is None:
            raise KeyError(f"task {task_id} does not exist")
        diff = service.plan_diff(task_id, from_version, to_version)
        return diff.model_dump(mode="json")

    @app.post("/api/tasks/{task_id}/amend")
    def amend(task_id: str, body: AmendIn):
        # Amendment is a PLANNED-state act only. On a CONFIRMED
        # task this returns 409: the route past a confirmed plan is
        # rejection and a fresh intake, never an edit.
        task, plan, changes = service.amend(
            task_id, body.operator, body.changes)
        return {"task": task.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
                "changes": [c.model_dump(mode="json") for c in changes]}

    @app.post("/api/tasks/{task_id}/reject")
    def reject(task_id: str, body: RejectIn):
        task = service.reject(task_id, body.operator)
        return task.model_dump(mode="json")

    @app.post("/api/tasks/{task_id}/replan")
    def replan(task_id: str, body: ReplanIn):
        task = service.replan(task_id, body.operator)
        plan = service.get_plan(task_id)
        return {"task": task.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json") if plan else None}

    @app.post("/api/tasks/{task_id}/confirm")
    def confirm(task_id: str, body: ConfirmIn):
        task = service.confirm(task_id, body.operator)
        return task.model_dump(mode="json")

    @app.post("/api/tasks/{task_id}/execute")
    def execute(task_id: str, body: ExecuteIn):
        task = service.execute(task_id, body.driver,
                               operator=body.operator)
        return task.model_dump(mode="json")

    @app.get("/api/tasks/{task_id}/attempts")
    def attempts(task_id: str):
        # Every execution attempt, numbered: the steps each one
        # completed, and for a failed attempt the step it stopped
        # at and the driver's error verbatim.
        if service.get_task(task_id) is None:
            raise KeyError(f"task {task_id} does not exist")
        return [a.model_dump(mode="json")
                for a in service.get_attempts(task_id)]

    @app.post("/api/tasks/{task_id}/resume")
    def resume(task_id: str, body: ResumeIn):
        # Pick a failed execution up where it stopped: only the
        # steps that never completed run, and only after the
        # basis is re-checked in code. A moved basis is a 409
        # naming what moved.
        task = service.resume_execution(task_id, body.operator)
        return task.model_dump(mode="json")

    @app.post("/api/tasks/{task_id}/restart")
    def restart(task_id: str, body: RestartIn):
        # Run the plan again from step 1, as a new attempt —
        # refused (409) once a CREATE step has completed, because
        # a created record is never created twice.
        task = service.restart_execution(task_id, body.operator)
        return task.model_dump(mode="json")

    @app.post("/api/tasks/{task_id}/abandon")
    def abandon(task_id: str, body: AbandonIn):
        # Close a failed execution as ABANDONED: an execution
        # act with a mandatory reason. The partial state stays
        # on the records, named — nothing is cleaned up.
        task = service.abandon(task_id, body.operator, body.reason)
        return task.model_dump(mode="json")

    # -- roles ---------------------------------------------------------
    @app.get("/api/roles")
    def roles():
        return {
            "roles": [r.model_dump(mode="json")
                      for r in service.list_roles()],
            "operators": service.list_operators(),
        }

    @app.get("/api/roles/{name}")
    def role(name: str):
        found = service.get_role(name)
        if found is None:
            raise KeyError(f"role {name} does not exist")
        return found.model_dump(mode="json")

    @app.put("/api/roles/{name}")
    def update_role(name: str, body: RoleUpdateIn):
        # A recorded act: who is changing the definition rides in
        # the body, and the service refuses anyone whose role may
        # not administer roles (409) and any definition the strict
        # loader refuses (422). The change lands in the roles audit.
        role = service.update_role(
            name, body.model_dump(mode="json",
                                  exclude={"operator"}),
            body.operator)
        return role.model_dump(mode="json")

    @app.get("/api/roles-audit")
    def roles_audit():
        return service.role_audit()

    # -- audit exports --------------------------------------------------
    # An export chains one scope's trail (exports.py): every
    # entry's hash over the previous hash and its own content,
    # under a header naming the scope, the exporter, the time,
    # the count, and the final hash — so an edited, dropped,
    # reordered, or truncated copy fails verification at the
    # first broken entry. Exporting is a recorded act behind
    # the identity gate (an unknown or deactivated exporter is
    # refused 409, on the record); the document covers the
    # trail as it stood, and the `audit_exported` entry the
    # act writes is the next export's first new link.
    def _export_response(document, fmt: str):
        if fmt == "csv":
            return PlainTextResponse(
                service.render_export_csv(document),
                media_type="text/csv")
        return document

    def _export_refused_format(fmt: str):
        return JSONResponse(status_code=422, content={
            "detail": f"format '{fmt}' is not an export "
                      f"rendering; use 'json' or 'csv'",
            "code": "format_not_supported"})

    @app.post("/api/roles-audit/export")
    def export_roles_audit(body: AuditExportIn):
        if body.format not in ("json", "csv"):
            return _export_refused_format(body.format)
        document = service.export_scope_audit(
            "roles", body.operator, rendering=body.format)
        return _export_response(document, body.format)

    @app.post("/api/audit/verify")
    def verify_audit_export(document: dict):
        # Grading a document is a read, not an act: the verdict
        # (intact, or broken at the first failing entry) writes
        # nothing anywhere.
        return service.verify_audit_export(document)

    # -- the operator directory --------------------------------------
    # Enrolment, role assignment, deactivation, and reactivation
    # are recorded supervisor acts (the acting supervisor rides in
    # the body); a refusal is a 409 with the service's reason, and
    # naming a role that does not exist is a 422. A name is never
    # deleted — deactivation is a state on its entry.
    @app.post("/api/operators", status_code=201)
    def enroll_operator(body: EnrollOperatorIn):
        return service.enroll_operator(
            body.operator, body.name, body.role)

    @app.put("/api/operators/{name}/role")
    def assign_operator_role(name: str, body: AssignRoleIn):
        return service.assign_operator_role(
            body.operator, name, body.role)

    @app.post("/api/operators/{name}/deactivate")
    def deactivate_operator(name: str, body: DirectoryActIn):
        return service.deactivate_operator(body.operator, name)

    @app.post("/api/operators/{name}/reactivate")
    def reactivate_operator(name: str, body: DirectoryActIn):
        return service.reactivate_operator(body.operator, name)

    @app.get("/api/operators/orphaned")
    def orphaned_work():
        # The unfinished work of deactivated operators — listed,
        # never reassigned by the listing. Pickup is the act.
        return service.orphaned_work()

    @app.post("/api/tasks/{task_id}/pickup")
    def pickup(task_id: str, body: PickupIn):
        # A supervisor takes over an orphaned task: the task's
        # operator becomes the supervisor, and the original
        # planner stays on the record.
        task = service.pickup_task(task_id, body.operator)
        return task.model_dump(mode="json")

    @app.get("/api/operators/report")
    def operator_report():
        # Per-operator history folded from the audit trail:
        # acts by kind, tasks planned/confirmed/executed, and
        # the refusals each name received, by code.
        return service.operator_report()

    @app.get("/api/operators/budget")
    def operator_budget():
        # Per-operator execution spend folded from the attempt
        # records: attempts run, tasks executed, steps consumed,
        # and the attempts the shell stopped for budget.
        return service.budget_report()

    @app.get("/api/attribution")
    def attribution():
        # The completeness check over the whole audit trail:
        # every act's actor must resolve to a directory name or
        # a named system component. Zero problems is the grade.
        return service.attribution_check()

    # -- the task library ---------------------------------------------
    # Templates are governed records: listed and read here with
    # their health folded in (computed on read from the pinned
    # examples), published in new versions only as recorded
    # supervisor acts, and instantiated by name and version.
    # Instantiation renders the pattern's text and states it as a
    # task through the ordinary intake — the plan the response
    # carries is the planner's derivation from that text, never
    # the template's own word.
    @app.get("/api/templates")
    def templates():
        return service.list_template_views()

    @app.get("/api/templates/{name}")
    def template(name: str):
        found = service.get_template(name)
        if found is None:
            raise KeyError(f"template {name} does not exist")
        return service.template_view(found)

    @app.get("/api/templates/{name}/versions/{version}")
    def template_version(name: str, version: int):
        found = service.get_template(name, version)
        if found is None:
            raise KeyError(
                f"template {name} v{version} does not exist")
        return service.template_view(found)

    @app.post("/api/templates", status_code=201)
    def publish_template(body: TemplatePublishIn):
        template = service.publish_template(
            body.operator,
            body.model_dump(mode="json", exclude={"operator"}))
        return template.model_dump(mode="json")

    @app.post("/api/templates/{name}/instantiate", status_code=201)
    def instantiate_template(name: str, body: TemplateInstantiateIn):
        task = service.instantiate_template(
            name, body.version, body.slots, body.operator)
        plan = service.get_plan(task.task_id)
        return {"task": task.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json") if plan else None}

    @app.get("/api/templates-audit")
    def templates_audit():
        return service.template_audit()

    @app.post("/api/templates-audit/export")
    def export_templates_audit(body: AuditExportIn):
        if body.format not in ("json", "csv"):
            return _export_refused_format(body.format)
        document = service.export_scope_audit(
            "templates", body.operator, rendering=body.format)
        return _export_response(document, body.format)

    # -- batches ------------------------------------------------------
    # A batch groups tasks submitted together. Creation states
    # each member through ordinary intake; the other routes are
    # reads — the run sheet and the report, both folded from the
    # member records. There is deliberately NO batch decision
    # route: no /confirm, no /execute, nothing that acts on the
    # members as a group. Confirmation and execution stay per
    # task, at /api/tasks/{task_id}/..., under the same gates.
    @app.post("/api/batches", status_code=201)
    def create_batch(body: BatchCreateIn):
        batch = service.create_batch(body.operator, body.members)
        return service.batch_view(batch)

    @app.get("/api/batches")
    def batches():
        return [service.batch_view(b) for b in service.list_batches()]

    @app.get("/api/batches/{batch_id}")
    def batch(batch_id: str):
        found = service.get_batch(batch_id)
        if found is None:
            raise KeyError(f"batch {batch_id} does not exist")
        return service.batch_view(found)

    @app.get("/api/batches/{batch_id}/run-sheet")
    def batch_run_sheet(batch_id: str):
        # The work list: each member's state, the next act it
        # needs, and who may take it. The sheet names acts; the
        # acts themselves are taken on the member tasks.
        found = service.get_batch(batch_id)
        if found is None:
            raise KeyError(f"batch {batch_id} does not exist")
        return service.batch_run_sheet(found)

    @app.get("/api/batches/{batch_id}/report")
    def batch_report(batch_id: str):
        # Folded from the member records at the moment of
        # asking: counts by state, the credited total from the
        # verified members' verification reports, the
        # discrepancies and failures still open, and whether
        # the batch is complete or who still owes an act.
        found = service.get_batch(batch_id)
        if found is None:
            raise KeyError(f"batch {batch_id} does not exist")
        return service.batch_report(found)

    @app.get("/api/batches-audit")
    def batches_audit():
        return service.batch_audit()

    @app.post("/api/batches/{batch_id}/audit/export")
    def export_batch_audit(batch_id: str, body: AuditExportIn):
        # The batch's trail: its own scope entries and every
        # member task's trail, interleaved in true order.
        if body.format not in ("json", "csv"):
            return _export_refused_format(body.format)
        if service.get_batch(batch_id) is None:
            raise KeyError(f"batch {batch_id} does not exist")
        document = service.export_batch_audit(
            batch_id, body.operator, rendering=body.format)
        return _export_response(document, body.format)

    @app.post("/api/tasks/{task_id}/steps/{seq}/report")
    def report_step(task_id: str, seq: int, body: StepReportIn):
        task = service.report_step(task_id, seq, body)
        return task.model_dump(mode="json")

    @app.get("/api/tasks/{task_id}/audit")
    def audit(task_id: str):
        if service.get_task(task_id) is None:
            raise KeyError(f"task {task_id} does not exist")
        return service.audit(task_id)

    @app.post("/api/tasks/{task_id}/audit/export")
    def export_task_audit(task_id: str, body: AuditExportIn):
        if body.format not in ("json", "csv"):
            return _export_refused_format(body.format)
        if service.get_task(task_id) is None:
            raise KeyError(f"task {task_id} does not exist")
        document = service.export_task_audit(
            task_id, body.operator, rendering=body.format)
        return _export_response(document, body.format)

    # -- the live bridge ---------------------------------------------------
    @app.get("/api/agent-config")
    def agent_config():
        """What the console page needs for `new PageAgent({...})`.

        Field names follow the library's own LLMConfig (verified
        against page-agent@1.12.4): baseURL and model required,
        apiKey optional. `maxSteps` is the shell's governed
        execution-step ceiling (Policy) served under the library's
        name — one source of truth for the number, so the in-page
        agent's own step budget and the shell's per-attempt budget
        are the same figure, set in one place."""
        cfg = settings.page_agent
        return {
            "model": cfg.model,
            "baseURL": cfg.base_url,
            "apiKey": cfg.api_key,
            "language": cfg.language,
            "maxSteps": settings.policy.max_execution_steps,
            "configured": cfg.configured,
        }

    # -- the console page ---------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def console():
        return (STATIC_DIR / "orderdesk.html").read_text()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app(seed=True)
