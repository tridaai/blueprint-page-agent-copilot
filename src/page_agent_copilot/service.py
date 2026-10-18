"""The governed task shell: intake -> plan -> confirm -> execute ->
verify, with the audit written as it goes.

The state machine is enforced here, in code, against the store —
not by the API layer and never by the agent:

    DRAFT -> PLANNED -> CONFIRMED -> VERIFIED | DISCREPANCY
    DRAFT -> REFUSED            (plan-time policy refusal)
    PLANNED -> PLANNED          (amendment: plan version n+1, the
                                 diff recorded, confirmation still
                                 owed on the new version)
    PLANNED | CONFIRMED -> REJECTED   (a human's no; the route
                                 onward is a fresh intake)
    CONFIRMED -> STALE          (a record the plan rests on changed
                                 before execution; nothing ran)
    STALE -> PLANNED            (re-plan: a fresh version derived
                                 from the records as they now stand,
                                 the stale version kept, superseded)
    DISCREPANCY -> (a corrective task)   (a linked follow-up task
                                 whose plan closes the named diffs,
                                 through the full gate cycle)
    DISCREPANCY -> DISCREPANCY_ACCEPTED  (a supervisor accepts the
                                 named deviation, with a reason)

Verification is an act with a stored record, not a recomputation:
it runs once, at the end of execution, re-reading the records
through the console read surface (reads.py) — never through the
mutation path the work went in by — and a second verification of
the same task is refused. A task in DISCREPANCY has exactly the
two recorded exits above; its report is never edited away, and no
exit rewrites it to VERIFIED.

Executing an unconfirmed plan raises GateError (the API maps it to
HTTP 409). Confirmation binds an exact plan version, and execution
runs that version. A task whose post-state does not match its
approved plan exactly lands in DISCREPANCY with the differing
fields and their steps named; no path reports it as a success.

Every act is also a *role* act (roles.py): the planner's role must
allow the plan's pages and fields before a plan exists; confirmation
needs a confirm-capable role held by someone other than the planner
(self-confirmation is refused for every role); amendment needs an
amend-capable role and stays inside that role's fields; and
execution re-checks the bound plan against the role definitions in
force at that moment — a role narrowed after confirmation holds the
task at CONFIRMED, refused and recorded, until the definition
allows it again. Every permission refusal is written to the audit
trail before it is raised.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from .config import Settings
from .drivers import DRIVER_LIVE, DRIVER_OFFLINE, OfflineDriver
from .models import (
    AmendChangeIn,
    AuditEntry,
    CorrectionEntry,
    Plan,
    PlanChange,
    PlanDiff,
    StepOutcome,
    StepReportIn,
    Task,
    TaskStatus,
    VerificationReport,
)
from .plandiff import diff_plans
from .planner import (
    amend_steps,
    build_corrective_steps,
    capture_preconditions,
    derive_plan,
    stale_records,
    summarize_steps,
)
from .policy import PlanRefusal
from .reads import ConsoleReads
from .roles import (
    DEFAULT_OPERATORS,
    DEFAULT_ROLES,
    ROLE_AUDIT_SCOPE,
    RoleDefinition,
    RoleError,
    describe_problem,
    load_role,
    operator_named,
    plan_problems,
    role_diff,
)
from .store import Store
from .verify import run_verification


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class GateError(Exception):
    """A state-machine refusal (HTTP 409 at the API)."""


class CopilotService:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        # The console read surface over the same store: the API
        # serves the console through it, and verification re-reads
        # through it — never through the mutation path.
        self.reads = ConsoleReads(store)
        self._lock = threading.Lock()

    # -- setup ------------------------------------------------------
    def seed(self) -> bool:
        from .dataset import seed_records

        return self.store.seed_if_empty(seed_records())

    # -- audit --------------------------------------------------------
    def _audit(self, task_id: str, actor: str, event: str,
               detail: Optional[dict] = None) -> None:
        self.store.add_audit(AuditEntry(
            task_id=task_id, ts=_now(), actor=actor, event=event,
            detail=detail or {}).model_dump(mode="json"))

    def audit(self, task_id: str) -> list[dict]:
        return self.store.list_audit(task_id)

    # -- roles ----------------------------------------------------------
    def _ensure_roles(self) -> None:
        """Seed the role definitions and the operator directory
        from the governed defaults when the store has none. Called
        on every role read, so the definitions in force are always
        the stored ones — an edit is a stored record, not a code
        change, and it is what the next act is checked against."""
        if not self.store.list_records("roles"):
            for raw in DEFAULT_ROLES:
                role = load_role(raw)  # the seed passes the loader
                self.store.upsert_record(
                    "roles", {"id": role.name,
                              **role.model_dump(mode="json")})
        if not self.store.list_records("operators"):
            for entry in DEFAULT_OPERATORS:
                self.store.upsert_record("operators", dict(entry))

    def list_roles(self) -> list[RoleDefinition]:
        self._ensure_roles()
        roles = []
        for raw in self.store.list_records("roles"):
            raw = {k: v for k, v in raw.items() if k != "id"}
            roles.append(load_role(raw))
        return sorted(roles, key=lambda r: r.name)

    def get_role(self, name: str) -> Optional[RoleDefinition]:
        return next((r for r in self.list_roles() if r.name == name),
                    None)

    def list_operators(self) -> list[dict]:
        self._ensure_roles()
        return self.store.list_records("operators")

    def role_of(self, operator: str) -> Optional[RoleDefinition]:
        """The role an operator name holds, or None: an unknown
        name holds no role and may do nothing."""
        self._ensure_roles()
        entry = self.store.get_record("operators", operator)
        if entry is None:
            return None
        return self.get_role(entry["role"])

    def role_audit(self) -> list[dict]:
        return self.store.list_audit(ROLE_AUDIT_SCOPE)

    def update_role(self, name: str, raw: dict,
                    actor: str) -> RoleDefinition:
        """A recorded role edit. The actor must hold a role with
        can_administer; the new definition must pass the strict
        loader; the change — who, when, and the field-level diff —
        is written to the roles audit scope before it takes effect.
        A refused edit stores nothing."""
        actor_role = self.role_of(actor)
        if actor_role is None or not actor_role.can_administer:
            raise GateError(
                f"'{actor}' may not edit role definitions: role "
                f"administration needs a role with can_administer "
                f"(a supervisor)")
        current = self.get_role(name)
        if current is None:
            raise RoleError(
                "role_not_found",
                f"there is no role named '{name}' to edit")
        candidate = load_role({**raw, "name": name})
        changes = role_diff(current, candidate)
        if not changes:
            raise GateError(
                f"the edit to role '{name}' changes nothing")
        self.store.upsert_record(
            "roles", {"id": name, **candidate.model_dump(mode="json")})
        self._audit(ROLE_AUDIT_SCOPE, actor, "role_updated", {
            "role": name, "changes": changes})
        return candidate

    # -- intake + planning ---------------------------------------------
    def intake(self, text: str, operator: str) -> Task:
        task_id = f"TASK-{uuid.uuid4().hex[:8].upper()}"
        task = Task(task_id=task_id, text=text, operator=operator,
                    created_at=_now(), updated_at=_now())
        self.store.put_task(task.model_dump(mode="json"))
        self._audit(task_id, operator, "task_created", {"text": text})
        try:
            # The planner's role is checked before the plan exists:
            # a name with no role, or a role that may not plan, is
            # refused here — at plan time, recorded like any other
            # plan refusal.
            role = self.role_of(operator)
            if role is None:
                raise PlanRefusal(
                    "unknown_operator",
                    f"refused: '{operator}' holds no role in the "
                    f"operator directory; an unknown operator may "
                    f"not plan work")
            if not role.can_plan:
                raise PlanRefusal(
                    "role_cannot_plan",
                    f"refused: role '{role.name}' may not plan work; "
                    f"planning is not among its action classes")
            plan = derive_plan(text, self.store, self.settings.policy,
                               task_id)
            # And the derived plan must fit the role's allowlist —
            # every page it works on, every field it touches.
            problems = plan_problems(role, plan)
            if problems:
                first = problems[0]
                raise PlanRefusal(
                    first["problem"],
                    f"refused: {describe_problem(role, first)}; "
                    f"the plan exceeds the planner's role")
        except PlanRefusal as refusal:
            task.status = TaskStatus.REFUSED
            task.refusal_reason = f"{refusal.code}: {refusal.message}"
            task.updated_at = _now()
            self.store.put_task(task.model_dump(mode="json"))
            self._audit(task_id, "policy", "plan_refused", {
                "code": refusal.code, "reason": refusal.message})
            return task
        plan.created_at = _now()
        plan.preconditions = capture_preconditions(self.store, plan)
        self.store.put_plan(plan.model_dump(mode="json"))
        task.status = TaskStatus.PLANNED
        task.plan_version = plan.version
        task.updated_at = _now()
        self.store.put_task(task.model_dump(mode="json"))
        self._audit(task_id, "planner", "plan_created", {
            "plan_version": plan.version, "task_type": plan.task_type,
            "summary": plan.summary, "steps": len(plan.steps)})
        return task

    # -- reads ----------------------------------------------------------
    def get_task(self, task_id: str) -> Optional[Task]:
        raw = self.store.get_task(task_id)
        return Task(**raw) if raw else None

    def list_tasks(self) -> list[Task]:
        return [Task(**t) for t in self.store.list_tasks()]

    def get_plan(self, task_id: str,
                 version: Optional[int] = None) -> Optional[Plan]:
        raw = (self.store.get_plan(task_id, version)
               if version is not None
               else self.store.latest_plan(task_id))
        return Plan(**raw) if raw else None

    def list_plans(self, task_id: str) -> list[Plan]:
        plans: list[Plan] = []
        version = 1
        while True:
            plan = self.get_plan(task_id, version)
            if plan is None:
                return plans
            plans.append(plan)
            version += 1

    def plan_diff(self, task_id: str, from_version: int,
                  to_version: int) -> PlanDiff:
        self._require_task(task_id)
        old = self.get_plan(task_id, from_version)
        new = self.get_plan(task_id, to_version)
        if old is None or new is None:
            missing = from_version if old is None else to_version
            raise GateError(
                f"task {task_id} has no plan version {missing} to diff")
        return diff_plans(old, new)

    def _store_plan_version(self, task: Task, plan: Plan) -> None:
        """Store a new plan version for the task, marking the
        version it replaces as superseded (kept, readable, never
        executed again — execution binds a confirmed version, and a
        superseded one is never the latest a confirmation can name).
        """
        previous = self.get_plan(task.task_id)
        if previous is not None and previous.version != plan.version:
            previous.superseded = True
            self.store.put_plan(previous.model_dump(mode="json"))
        self.store.put_plan(plan.model_dump(mode="json"))
        task.plan_version = plan.version

    def _require_task(self, task_id: str) -> Task:
        task = self.get_task(task_id)
        if task is None:
            raise GateError(f"task {task_id} does not exist")
        return task

    def _save(self, task: Task) -> Task:
        task.updated_at = _now()
        self.store.put_task(task.model_dump(mode="json"))
        return task

    # -- plan amendment, rejection, re-planning ---------------------------
    def amend(self, task_id: str, operator: str,
              changes: list[AmendChangeIn]) -> tuple[Task, Plan,
                                                     list[PlanChange]]:
        """Amend a PLANNED task's plan. The amendment becomes plan
        version n+1, with its field-level diff recorded in the
        audit; the task stays PLANNED and owes a fresh confirmation
        of the new version. Amending a confirmed task is refused:
        a confirmation binds the version the human saw, and the
        route past a confirmed plan is rejection + a fresh intake,
        never an edit. A value policy would refuse in a fresh plan
        is refused here too (PlanRefusal) and changes nothing."""
        task = self._require_task(task_id)
        if task.status != TaskStatus.PLANNED:
            raise GateError(
                f"task {task_id} is {task.status.value}; only a PLANNED "
                f"task's plan can be amended — a confirmed plan is "
                f"executed as confirmed or rejected, never amended")
        role = self.role_of(operator)
        if role is None or not role.can_amend:
            reason = (f"'{operator}' holds no role"
                      if role is None else
                      f"role '{role.name}' may not amend plans")
            self._audit(task_id, operator, "amendment_refused", {
                "code": "role_cannot_amend", "reason": reason})
            raise GateError(
                f"amendment to task {task_id} refused: {reason}")
        current = self.get_plan(task_id)
        assert current is not None
        new_steps, recorded = amend_steps(
            current, changes, self.settings.policy, self.store)
        if not recorded:
            raise GateError(
                f"the amendment to task {task_id} changes nothing; "
                f"plan v{current.version} stands")
        # The amended fields must also be the amender's to touch:
        # an amendment may not widen a plan into a field the
        # amender's own role holds back, whoever planned it.
        steps_by_seq = {s.seq: s for s in new_steps}
        for change in recorded:
            step = steps_by_seq[change.seq]
            if not role.allows_field(step.record_kind, change.field):
                problem = {"seq": step.seq, "page": step.page,
                           "record_kind": step.record_kind,
                           "field": change.field,
                           "problem": "field_not_permitted"}
                self._audit(task_id, operator, "amendment_refused", {
                    "code": "field_not_permitted",
                    "reason": describe_problem(role, problem)})
                raise PlanRefusal(
                    "field_not_permitted",
                    f"refused: {describe_problem(role, problem)}; "
                    f"an amendment stays inside the amender's role")
        amended = current.model_copy(deep=True)
        amended.version = current.version + 1
        amended.steps = new_steps
        amended.summary = summarize_steps(new_steps)
        amended.created_at = _now()
        amended.superseded = False
        amended.preconditions = capture_preconditions(
            self.store, amended)
        self._store_plan_version(task, amended)
        self._save(task)
        self._audit(task_id, operator, "plan_amended", {
            "from_version": current.version,
            "to_version": amended.version,
            "changes": [c.model_dump(mode="json") for c in recorded]})
        return task, amended, recorded

    def reject(self, task_id: str, operator: str) -> Task:
        """A human's no to a planned or confirmed task. Terminal:
        the operator states a fresh task if the work is still
        wanted, so no rejected plan can be revived into execution."""
        task = self._require_task(task_id)
        if task.status not in (TaskStatus.PLANNED, TaskStatus.CONFIRMED):
            raise GateError(
                f"task {task_id} is {task.status.value}; only a PLANNED "
                f"or CONFIRMED task can be rejected")
        task.status = TaskStatus.REJECTED
        self._save(task)
        self._audit(task_id, operator, "plan_rejected", {
            "plan_version": task.plan_version})
        return task

    def replan(self, task_id: str, operator: str) -> Task:
        """Re-derive a STALE task's plan from the records as they
        now stand. The stale version is kept and marked superseded;
        the task returns to PLANNED under the new version, owing a
        fresh confirmation. If the records as they now stand no
        longer support a plan at all, the task is REFUSED with the
        planner's reason — a re-plan cannot launder a refusal."""
        task = self._require_task(task_id)
        if task.status != TaskStatus.STALE:
            raise GateError(
                f"task {task_id} is {task.status.value}; only a STALE "
                f"task can be re-planned")
        previous = self.get_plan(task_id)
        assert previous is not None
        try:
            plan = derive_plan(task.text, self.store,
                               self.settings.policy, task_id)
        except PlanRefusal as refusal:
            task.status = TaskStatus.REFUSED
            task.refusal_reason = f"{refusal.code}: {refusal.message}"
            task.stale_records = []
            self._save(task)
            self._audit(task_id, "policy", "plan_refused", {
                "code": refusal.code, "reason": refusal.message,
                "during": "replan"})
            return task
        plan.version = previous.version + 1
        plan.created_at = _now()
        plan.preconditions = capture_preconditions(self.store, plan)
        self._store_plan_version(task, plan)
        task.status = TaskStatus.PLANNED
        task.confirmed_by = None
        task.confirmed_version = None
        task.stale_records = []
        self._save(task)
        self._audit(task_id, operator, "plan_replanned", {
            "from_version": previous.version,
            "to_version": plan.version,
            "superseded_version": previous.version})
        return task

    # -- the gate ---------------------------------------------------------
    def confirm(self, task_id: str, operator: str) -> Task:
        task = self._require_task(task_id)
        if task.status != TaskStatus.PLANNED:
            raise GateError(
                f"task {task_id} is {task.status.value}; only a PLANNED "
                f"task can be confirmed")
        # The confirmer must hold a confirm-capable role, and must
        # not be the planner: self-confirmation is refused for
        # every role (names compare normalised, so a spacing or
        # casing variant of the planner's name is still the
        # planner). Both refusals are recorded before they raise.
        role = self.role_of(operator)
        if role is None or not role.can_confirm:
            code = ("unknown_operator" if role is None
                    else "role_cannot_confirm")
            reason = (f"'{operator}' holds no role" if role is None
                      else f"role '{role.name}' may not confirm plans")
            self._audit(task_id, operator, "confirmation_refused", {
                "code": code, "reason": reason,
                "planner": task.operator,
                "plan_version": task.plan_version})
            raise GateError(
                f"confirmation of task {task_id} refused: {reason}")
        if operator_named(operator) == operator_named(task.operator):
            self._audit(task_id, operator, "confirmation_refused", {
                "code": "self_confirmation",
                "reason": "the planner may not confirm their own plan",
                "planner": task.operator,
                "plan_version": task.plan_version})
            raise GateError(
                f"confirmation of task {task_id} refused: "
                f"'{operator}' planned this task; a plan is "
                f"confirmed by a second person, never its planner")
        task.status = TaskStatus.CONFIRMED
        task.confirmed_by = operator
        # The confirmation binds this exact version. Execution loads
        # it by number, so nothing that happens to the plan store
        # afterwards can widen what the human approved.
        task.confirmed_version = task.plan_version
        self._save(task)
        self._audit(task_id, operator, "plan_confirmed", {
            "plan_version": task.plan_version,
            "confirmed_version": task.confirmed_version})
        return task

    def _bound_plan(self, task: Task) -> Plan:
        """The plan version this task's execution is bound to: the
        confirmed version once confirmed, else the current plan."""
        version = task.confirmed_version or task.plan_version
        plan = (self.get_plan(task.task_id, version)
                if version is not None else self.get_plan(task.task_id))
        assert plan is not None
        return plan

    # -- execution ---------------------------------------------------------
    def execute(self, task_id: str, driver: str = DRIVER_OFFLINE,
                *, tamper: Optional[dict[int, str]] = None,
                operator: Optional[str] = None) -> Task:
        task = self._require_task(task_id)
        if task.status != TaskStatus.CONFIRMED:
            raise GateError(
                f"task {task_id} is {task.status.value}; execution "
                f"requires a CONFIRMED plan — an unconfirmed plan "
                f"cannot execute")
        if task.driver is not None:
            raise GateError(
                f"task {task_id} already started execution with "
                f"driver '{task.driver}'")
        plan = self._bound_plan(task)
        # Permissions are re-checked at execution, against the role
        # definitions in force NOW: the executor must hold an
        # execute-capable role, and the bound plan must still fit
        # both the executor's allowlist and the planner's. A role
        # narrowed after confirmation therefore holds the task —
        # refused, recorded, still CONFIRMED, nothing run — until
        # the definitions allow the plan again.
        executor = operator if operator is not None else task.operator
        checks: list[tuple[str, Optional[RoleDefinition]]] = [
            ("planner", self.role_of(task.operator)),
            ("executor", self.role_of(executor)),
        ]
        for who, role in checks:
            if role is None:
                self._audit(task_id, executor,
                            "execution_refused", {
                                "code": "unknown_operator",
                                "who": who,
                                "reason": f"the {who} holds no role",
                                "plan_version": plan.version})
                raise GateError(
                    f"execution of task {task_id} refused: the "
                    f"{who} of this task holds no role in the "
                    f"operator directory")
            if who == "executor" and not role.can_execute:
                self._audit(task_id, executor,
                            "execution_refused", {
                                "code": "role_cannot_execute",
                                "who": who, "role": role.name,
                                "reason": f"role '{role.name}' may "
                                          f"not execute plans",
                                "plan_version": plan.version})
                raise GateError(
                    f"execution of task {task_id} refused: role "
                    f"'{role.name}' may not execute plans")
            problems = plan_problems(role, plan)
            if problems:
                first = problems[0]
                self._audit(task_id, executor,
                            "execution_refused", {
                                "code": first["problem"], "who": who,
                                "role": role.name,
                                "field": first["field"],
                                "record_kind": first["record_kind"],
                                "reason": describe_problem(role, first),
                                "plan_version": plan.version})
                raise GateError(
                    f"execution of task {task_id} refused: "
                    f"{describe_problem(role, first)} — the role "
                    f"definitions in force at execution no longer "
                    f"allow the {who}'s plan; the task is held at "
                    f"CONFIRMED")
        # Staleness: the plan was derived from records as they stood
        # at plan time (its preconditions). If any of those records
        # changed since, executing would write on a basis that no
        # longer exists — refuse before anything runs, name the
        # records, and park the task in STALE for a re-plan.
        changed = stale_records(self.store, plan)
        if changed:
            task.status = TaskStatus.STALE
            task.stale_records = changed
            self._save(task)
            self._audit(task_id, task.operator, "execution_stale", {
                "plan_version": plan.version,
                "changed_records": changed})
            raise GateError(
                f"task {task_id} is stale: {', '.join(changed)} "
                f"changed since plan v{plan.version} was derived; "
                f"execution refused — re-plan against the records "
                f"as they now stand")
        task.driver = driver
        self._save(task)
        if driver == DRIVER_LIVE:
            # The agent runs in the operator's browser; the bridge
            # reports each step back. Verification runs when the
            # final step lands (see report_step).
            self._audit(task_id, task.operator, "execution_started", {
                "driver": driver, "plan_version": plan.version,
                "steps": len(plan.steps)})
            return task
        if driver != DRIVER_OFFLINE:
            raise GateError(f"unknown driver '{driver}'")
        runner = OfflineDriver(tamper=tamper)
        outcomes: list[StepOutcome] = []
        created_ids: dict[int, str] = {}
        for step in plan.steps:
            outcome = runner.run_step(self.store, task, plan, step)
            outcomes.append(outcome)
            if outcome.after.get("id"):
                created_ids[step.seq] = str(outcome.after["id"])
            self._audit(task_id, driver, "step_executed", {
                "seq": step.seq, "plan_version": plan.version,
                "before": outcome.before, "after": outcome.after,
                "detail": outcome.detail})
        self._audit(task_id, driver, "execution_completed", {
            "driver": driver, "steps": len(outcomes)})
        return self._verify(task, plan, created_ids)

    def report_step(self, task_id: str, seq: int,
                    report: StepReportIn) -> Task:
        """Record one live step report from the browser bridge.

        The report is advisory: it is written to the audit, and when
        the final step lands the shell verifies against the store —
        the report's own `ok` is never treated as evidence."""
        task = self._require_task(task_id)
        if task.status != TaskStatus.CONFIRMED or task.driver != DRIVER_LIVE:
            raise GateError(
                f"task {task_id} is not awaiting live step reports "
                f"(status {task.status.value}, driver {task.driver})")
        plan = self._bound_plan(task)
        if seq not in {s.seq for s in plan.steps}:
            raise GateError(f"plan for task {task_id} has no step {seq}")
        prior = [e for e in self.audit(task_id)
                 if e["event"] == "step_reported"
                 and e["detail"].get("seq") == seq]
        if prior:
            raise GateError(
                f"step {seq} of task {task_id} was already reported")
        self._audit(task_id, DRIVER_LIVE, "step_reported", {
            "seq": seq, "ok": report.ok, "detail": report.detail,
            "plan_version": plan.version})
        reported = {e["detail"].get("seq") for e in self.audit(task_id)
                    if e["event"] == "step_reported"}
        if reported != {s.seq for s in plan.steps}:
            return task
        self._audit(task_id, DRIVER_LIVE, "execution_completed", {
            "driver": DRIVER_LIVE, "steps": len(plan.steps)})
        created_ids: dict[int, str] = {}
        for memo in self.reads.memos_for_task(task_id):
            for step in plan.steps:
                if step.record_kind == "credit_memos":
                    created_ids[step.seq] = str(memo["id"])
        return self._verify(task, plan, created_ids)

    # -- verification -------------------------------------------------------
    def _verify(self, task: Task, plan: Plan,
                created_ids: dict[int, str]) -> Task:
        """The verification act: re-read through the console read
        surface, diff field by field plus the linkage checks, and
        store the report as a record (kind `verifications`, keyed
        by task id — the same records mechanism the roles ride).
        The verdict also lands in the audit trail, as before."""
        report = run_verification(
            self.reads, plan, created_ids, task_id=task.task_id,
            driver=task.driver or "",
            audit=self.audit(task.task_id), verified_at=_now())
        self.store.upsert_record("verifications", {
            "id": task.task_id, **report.model_dump(mode="json")})
        if report.ok:
            task.status = TaskStatus.VERIFIED
            self._save(task)
            self._audit(task.task_id, "verifier", "verification_passed", {
                "plan_version": plan.version, "checked": report.checked,
                "read_path": report.read_path})
        else:
            task.status = TaskStatus.DISCREPANCY
            self._save(task)
            self._audit(task.task_id, "verifier", "verification_failed", {
                "plan_version": plan.version, "checked": report.checked,
                "read_path": report.read_path,
                "diffs": [d.model_dump(mode="json") for d in report.diffs]})
        return task

    def _stored_verification(
            self, task_id: str) -> Optional[VerificationReport]:
        """The report exactly as stored — no read-time composition."""
        raw = self.store.get_record("verifications", task_id)
        if raw is None:
            return None
        raw = {k: v for k, v in raw.items() if k != "id"}
        return VerificationReport(**raw)

    def get_verification(
            self, task_id: str) -> Optional[VerificationReport]:
        """The task's verification report, with its correction
        chain composed at read time from the task records: the
        corrective tasks this task spawned (via their
        parent_task_id), each with its status and its own verdict.
        The stored report is never rewritten — a discrepancy's
        record stands as it was written, chain and all."""
        report = self._stored_verification(task_id)
        if report is None:
            return None
        report = report.model_copy(deep=True)
        children = [t for t in self.list_tasks()
                    if t.parent_task_id == task_id]
        children.sort(key=lambda t: (t.created_at, t.task_id))
        entries: list[CorrectionEntry] = []
        for child in children:
            child_report = self._stored_verification(child.task_id)
            entries.append(CorrectionEntry(
                task_id=child.task_id, status=child.status.value,
                plan_version=child.plan_version,
                verification_ok=(child_report.ok
                                 if child_report else None)))
        report.corrections = entries
        return report

    def verify_task(self, task_id: str, operator: str) -> VerificationReport:
        """A request to verify (again). Verification is an act
        with a record, not a recomputation: it ran once, at the end
        of execution, and its report is the evidence. Asking again
        is refused — recorded, like every refusal — whether the
        task already has its report or has not executed at all."""
        task = self._require_task(task_id)
        report = self._stored_verification(task_id)
        if report is not None:
            self._audit(task_id, operator, "reverification_refused", {
                "code": "already_verified",
                "reason": "verification already ran and its report "
                          "is recorded; a verdict is not recomputed",
                "verified_at": report.verified_at,
                "plan_version": report.plan_version})
            raise GateError(
                f"task {task_id} was already verified at "
                f"{report.verified_at} against plan "
                f"v{report.plan_version}; the recorded report is "
                f"the evidence and verification is not re-run")
        raise GateError(
            f"task {task_id} has nothing to verify: verification "
            f"runs at the end of execution and is recorded then "
            f"(the task is {task.status.value})")

    # -- the discrepancy exits -------------------------------------------
    def spawn_corrective(self, task_id: str, operator: str) -> Task:
        """Exit one from a DISCREPANCY: a corrective task.

        The corrective is a new task, linked both ways (its
        `parent_task_id`, the parent's `corrective_task_ids`),
        whose plan is derived from the parent's verification
        report to close the named diffs — only those steps, only
        those fields. It then goes through the whole cycle on its
        own: confirmation by a second person, execution, its own
        verification. The parent stays DISCREPANCY; its report is
        never edited, and its correction chain is composed at read
        time from the task records.

        The spawner is the corrective plan's planner, so the R3
        role rules apply whole: the name must hold a plan-capable
        role, and the corrective plan must fit that role's
        allowlist (a corrective for a supervisor-only field is a
        supervisor's to spawn). A difference no plan can close —
        a memo created with wrong values, a linkage field no plan
        may set — refuses the spawn, recorded, with the reason;
        the remaining exit is accepting the deviation."""
        task = self._require_task(task_id)
        if task.status != TaskStatus.DISCREPANCY:
            raise GateError(
                f"task {task_id} is {task.status.value}; only a "
                f"DISCREPANCY task can spawn a corrective task")
        role = self.role_of(operator)
        if role is None or not role.can_plan:
            code = ("unknown_operator" if role is None
                    else "role_cannot_plan")
            reason = (f"'{operator}' holds no role" if role is None
                      else f"role '{role.name}' may not plan work")
            self._audit(task_id, operator, "corrective_refused", {
                "code": code, "reason": reason})
            raise GateError(
                f"corrective task for {task_id} refused: {reason}")
        for child_id in task.corrective_task_ids:
            child = self.get_task(child_id)
            if child is not None and child.status == TaskStatus.VERIFIED:
                self._audit(task_id, operator, "corrective_refused", {
                    "code": "already_corrected",
                    "reason": f"corrective task {child_id} already "
                              f"closed these differences and "
                              f"verified",
                    "corrective_task_id": child_id})
                raise GateError(
                    f"task {task_id} already has a verified "
                    f"corrective task ({child_id}); its differences "
                    f"are closed")
        report = self._stored_verification(task_id)
        assert report is not None  # a DISCREPANCY verdict wrote one
        parent_plan = self._bound_plan(task)
        try:
            steps = build_corrective_steps(parent_plan, report)
        except PlanRefusal as refusal:
            self._audit(task_id, operator, "corrective_refused", {
                "code": refusal.code, "reason": refusal.message})
            raise GateError(refusal.message)
        child_id = f"TASK-{uuid.uuid4().hex[:8].upper()}"
        plan = Plan(
            task_id=child_id, version=1, task_type="corrective",
            summary=summarize_steps(steps), steps=steps)
        problems = plan_problems(role, plan)
        if problems:
            first = problems[0]
            self._audit(task_id, operator, "corrective_refused", {
                "code": first["problem"],
                "reason": describe_problem(role, first)})
            raise GateError(
                f"corrective task for {task_id} refused: "
                f"{describe_problem(role, first)} — the corrective "
                f"plan must fit the spawner's role")
        plan.created_at = _now()
        plan.preconditions = capture_preconditions(self.store, plan)
        child = Task(
            task_id=child_id,
            text=(f"Corrective for {task_id}: close the differences "
                  f"verification named in plan v{parent_plan.version}"),
            operator=operator, status=TaskStatus.PLANNED,
            plan_version=1, parent_task_id=task_id,
            created_at=_now(), updated_at=_now())
        self.store.put_task(child.model_dump(mode="json"))
        self.store.put_plan(plan.model_dump(mode="json"))
        self._audit(child_id, operator, "task_created", {
            "text": child.text, "parent_task_id": task_id,
            "source": "corrective"})
        self._audit(child_id, "planner", "plan_created", {
            "plan_version": 1, "task_type": "corrective",
            "summary": plan.summary, "steps": len(steps)})
        task.corrective_task_ids.append(child_id)
        self._save(task)
        self._audit(task_id, operator, "corrective_spawned", {
            "corrective_task_id": child_id,
            "closes": [d.model_dump(mode="json")
                       for d in report.diffs]})
        return child

    def accept_deviation(self, task_id: str, operator: str,
                         reason: str) -> Task:
        """Exit two from a DISCREPANCY: a supervisor accepts the
        named deviation, with a reason, and the task closes as
        DISCREPANCY_ACCEPTED — a distinct terminal state, never
        rewritten to VERIFIED, with the original report untouched.

        Supervisor-only in role terms: the accepting name must hold
        a role with can_administer (the supervisor's capability).
        The reason is mandatory — an acceptance names the diffs it
        accepts (they ride in the audit entry from the stored
        report) and states why; an empty reason is refused, and
        every refusal is recorded before it raises."""
        task = self._require_task(task_id)
        if task.status != TaskStatus.DISCREPANCY:
            raise GateError(
                f"task {task_id} is {task.status.value}; only a "
                f"DISCREPANCY task has a deviation to accept")
        if not (reason or "").strip():
            self._audit(task_id, operator, "deviation_refused", {
                "code": "reason_required",
                "reason": "accepting a deviation requires a stated "
                          "reason"})
            raise GateError(
                f"deviation on task {task_id} not accepted: a "
                f"reason is required — the acceptance names the "
                f"differences and states why they stand")
        role = self.role_of(operator)
        if role is None or not role.can_administer:
            why = (f"'{operator}' holds no role" if role is None
                   else f"role '{role.name}' may not accept "
                        f"deviations")
            self._audit(task_id, operator, "deviation_refused", {
                "code": "not_a_supervisor", "reason": why})
            raise GateError(
                f"deviation on task {task_id} not accepted: {why}; "
                f"accepting a deviation is a supervisor act")
        report = self._stored_verification(task_id)
        diffs = report.diffs if report else []
        task.status = TaskStatus.DISCREPANCY_ACCEPTED
        task.deviation_accepted_by = operator
        task.deviation_reason = reason.strip()
        self._save(task)
        self._audit(task_id, operator, "deviation_accepted", {
            "reason": task.deviation_reason,
            "diffs": [d.model_dump(mode="json") for d in diffs]})
        return task
