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
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .logging_setup import new_request_id, setup_logging
from .models import (
    PAYMENT_FIELDS,
    AbandonIn,
    AcceptDeviationIn,
    AmendIn,
    ConfirmIn,
    ConsoleSaveIn,
    CorrectiveIn,
    ExecuteIn,
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

    @app.post("/api/tasks/{task_id}/steps/{seq}/report")
    def report_step(task_id: str, seq: int, body: StepReportIn):
        task = service.report_step(task_id, seq, body)
        return task.model_dump(mode="json")

    @app.get("/api/tasks/{task_id}/audit")
    def audit(task_id: str):
        if service.get_task(task_id) is None:
            raise KeyError(f"task {task_id} does not exist")
        return service.audit(task_id)

    # -- the live bridge ---------------------------------------------------
    @app.get("/api/agent-config")
    def agent_config():
        """What the console page needs for `new PageAgent({...})`.

        Field names follow the library's own LLMConfig (verified
        against page-agent@1.12.4): baseURL and model required,
        apiKey optional."""
        cfg = settings.page_agent
        return {
            "model": cfg.model,
            "baseURL": cfg.base_url,
            "apiKey": cfg.api_key,
            "language": cfg.language,
            "maxSteps": cfg.max_steps,
            "configured": cfg.configured,
        }

    # -- the console page ---------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def console():
        return (STATIC_DIR / "orderdesk.html").read_text()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app(seed=True)
