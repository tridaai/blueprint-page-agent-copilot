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
    CONFIRMED -> EXECUTION_FAILED  (a driver step failed mid-plan;
                                 execution stopped at that step, the
                                 completed steps stand as recorded)
    EXECUTION_FAILED -> CONFIRMED  (resume: only the steps that
                                 never completed run, as a new
                                 attempt, after the basis is
                                 re-checked — or a live attempt
                                 re-armed for its remaining steps)
    EXECUTION_FAILED -> VERIFIED | DISCREPANCY | EXECUTION_FAILED
                                 (a resumed or restarted attempt
                                 runs its course like any execution)
    EXECUTION_FAILED -> ABANDONED  (an operator closes the failure
                                 with a reason; the partial state
                                 stays on the records, named)

Every execution is a numbered attempt, stored as a record: the
steps it completed with their before/after values, and — when it
failed — the failed step and the driver's error verbatim. An
attempt that is still running is the task's execution claim: a
second execute, resume, or restart while one is in flight is
refused. A failure never reports partial success and never rolls
anything back — the record says exactly what happened, and the
way forward is one of the recorded acts above.

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

Every act is also an *identity* act (roles.py, round 6). The
operator directory is governed: names are enrolled, assigned a
role, deactivated, and reactivated only through recorded
supervisor acts, and a deactivated name can perform no act at
all — every entry point refuses it, recorded, with the code
`operator_inactive`. Deactivation is a state, never a deletion:
the name stays in the directory so its history keeps resolving,
and its unfinished work is never reassigned or deleted — it
surfaces as orphaned work until a supervisor picks it up as a
recorded act, with the original planner kept on the task. Two
reads close the loop: an attribution check that scans the whole
audit trail for an act whose actor resolves neither to a
directory name (in any state) nor to one of the shell's named
system components, and a per-operator report folded from the
audit — never from counters — so what a name did, and what was
refused to it, reads off the same trail.

The task library (templates.py, round 7) sits in front of
intake, never beside it. A template is a governed record — a
task-text pattern with named slots, a note on when to use it,
and a version — seeded from defaults through a strict loader
and published in new versions only as recorded supervisor acts,
old versions kept, superseded, and still instantiable by name
and version. Instantiating one validates the slot values (a
record slot must name a record that exists; a money slot must
parse), renders the text, and hands it to `intake` unchanged:
the planner derives the plan from the rendered text through the
same grammar, so no template can smuggle a plan past the
planner, and a slot value that renders text policy refuses
produces the planner's own refusal on the task. Each version
pins examples — slot values plus the exact plan shape they must
derive — and a version's health is computed from them on read,
against the seeded records: a planner or policy change that
breaks a template's examples marks that version broken in the
library, by name and version.
"""

from __future__ import annotations

import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import InvalidOperation
from typing import Optional

from .config import Settings
from .drivers import DRIVER_LIVE, DRIVER_OFFLINE, OfflineDriver
from .models import (
    AmendChangeIn,
    AuditEntry,
    CorrectionEntry,
    ExecutionAttempt,
    Plan,
    PlanChange,
    PlanDiff,
    PlanStep,
    StepAction,
    StepOutcome,
    StepReportIn,
    Task,
    TaskStatus,
    VerificationReport,
    money,
)
from .plandiff import diff_plans
from .planner import (
    amend_steps,
    build_corrective_steps,
    capture_preconditions,
    derive_plan,
    record_hash,
    stale_records,
    step_targets,
    summarize_steps,
)
from .policy import PlanRefusal
from .reads import ConsoleReads
from .roles import (
    DEFAULT_OPERATORS,
    DEFAULT_ROLES,
    ROLE_AUDIT_SCOPE,
    SYSTEM_ACTORS,
    RoleDefinition,
    RoleError,
    describe_problem,
    load_role,
    operator_named,
    plan_problems,
    role_diff,
)
from .store import InMemoryStore, Store
from .templates import (
    DEFAULT_TEMPLATES,
    TEMPLATE_AUDIT_SCOPE,
    TaskTemplate,
    TemplateError,
    load_template,
    render_pattern,
    shape_detail,
)
from .verify import run_verification


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class GateError(Exception):
    """A state-machine refusal (HTTP 409 at the API)."""


def _template_content(template: TaskTemplate) -> dict:
    """A template version's content — pattern, description,
    slots, examples — the fields a publish may change. Two
    versions with equal content are the same template wearing two
    numbers, which a publish refuses."""
    return {
        "pattern": template.pattern,
        "description": template.description,
        "slots": [s.model_dump(mode="json") for s in template.slots],
        "examples": [e.model_dump(mode="json")
                     for e in template.examples],
    }


# The states in which a task still owes its operator an act —
# planned and awaiting confirmation, confirmed and not yet
# executed, stale and awaiting a re-plan, failed and awaiting a
# recovery act. Work in one of these states whose operator has
# been deactivated is orphaned work (see `orphaned_work`).
ORPHANED_STATUSES = (
    TaskStatus.PLANNED, TaskStatus.CONFIRMED, TaskStatus.STALE,
    TaskStatus.EXECUTION_FAILED,
)


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

    def directory_entry(self, operator: str) -> Optional[dict]:
        """The directory entry a name resolves to, or None.

        Resolution is normalised — a spacing or casing variant of
        an enrolled name is that name, at act time exactly as at
        enrolment — so the directory is scanned by normalised id
        rather than read by exact key. The entry carries the
        name's role and its active state; a deactivated name still
        resolves (its history stays attributable) even though it
        may perform no act."""
        wanted = operator_named(operator)
        if not wanted:
            return None
        for entry in self.list_operators():
            if operator_named(entry["id"]) == wanted:
                return entry
        return None

    @staticmethod
    def entry_active(entry: dict) -> bool:
        """Whether a directory entry may act. Entries seeded
        before the directory round carry no flag; an absent flag
        reads as active, and every write since carries it."""
        return bool(entry.get("active", True))

    def role_of(self, operator: str) -> Optional[RoleDefinition]:
        """The role an operator name holds, or None: an unknown
        name holds no role and may do nothing. A deactivated name
        still holds its role — the role is what the record says;
        whether the name may *act* is the active check every entry
        point runs beside this one."""
        entry = self.directory_entry(operator)
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
        actor_entry = self.directory_entry(actor)
        if actor_entry is not None and not self.entry_active(
                actor_entry):
            raise GateError(
                f"'{actor}' may not edit role definitions: the "
                f"name is deactivated (operator_inactive) — a "
                f"deactivated operator performs no acts")
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

    # -- the operator directory --------------------------------------
    # Enrolment, role assignment, deactivation, and reactivation
    # are recorded supervisor acts, kept in the same roles audit
    # scope as the role edits: the act, the actor, the target, and
    # the before/after ride the entry. A name is never deleted —
    # deactivation is a state on its entry — so the trail a name
    # left keeps resolving to it.

    def _directory_refused(self, actor: str, act: str, target: str,
                           code: str, reason: str) -> None:
        self._audit(ROLE_AUDIT_SCOPE, actor, "directory_refused", {
            "act": act, "target": target, "code": code,
            "reason": reason})
        raise GateError(f"{act} refused: {reason}")

    def _directory_actor(self, actor: str, act: str,
                         target: str) -> dict:
        """The gate every directory act passes: the acting name
        must be enrolled, active, and holding a role that may
        administer. Each refusal is recorded before it raises."""
        entry = self.directory_entry(actor)
        if entry is None:
            self._directory_refused(
                actor, act, target, "unknown_operator",
                f"'{actor}' holds no role in the operator "
                f"directory; directory administration is a "
                f"supervisor act")
        if not self.entry_active(entry):
            self._directory_refused(
                actor, act, target, "operator_inactive",
                f"'{actor}' is deactivated (operator_inactive) — "
                f"a deactivated operator performs no acts")
        role = self.get_role(entry["role"])
        if role is None or not role.can_administer:
            self._directory_refused(
                actor, act, target, "role_cannot_administer",
                f"'{actor}' may not administer the operator "
                f"directory: the act needs a role with "
                f"can_administer (a supervisor)")
        return entry

    def _directory_target(self, actor: str, act: str,
                          name: str) -> dict:
        entry = self.directory_entry(name)
        if entry is None:
            self._directory_refused(
                actor, act, name, "operator_not_found",
                f"there is no operator named '{name}' in the "
                f"directory")
        return entry

    def enroll_operator(self, actor: str, name: str,
                        role_name: str) -> dict:
        """Enrol a name in the directory, with a role. The name is
        stored normalised, and enrolling a name the directory
        already holds — active or deactivated — is refused: a
        deactivated name comes back by reactivation, not by a
        second enrolment that would fork its history."""
        canonical = operator_named(name)
        self._directory_actor(actor, "enrolment", canonical)
        if not canonical:
            self._directory_refused(
                actor, "enrolment", name, "invalid_name",
                "an operator name may not be empty")
        existing = self.directory_entry(canonical)
        if existing is not None:
            state = ("active" if self.entry_active(existing)
                     else "deactivated")
            self._directory_refused(
                actor, "enrolment", canonical, "operator_exists",
                f"'{canonical}' is already in the directory "
                f"({state}, role '{existing['role']}'); a name is "
                f"enrolled once — a deactivated name returns by "
                f"reactivation")
        if self.get_role(role_name) is None:
            raise RoleError(
                "role_not_found",
                f"there is no role named '{role_name}' to assign")
        entry = {"id": canonical, "role": role_name, "active": True}
        self.store.upsert_record("operators", entry)
        self._audit(ROLE_AUDIT_SCOPE, actor, "operator_enrolled", {
            "operator": canonical, "role": role_name})
        return entry

    def assign_operator_role(self, actor: str, name: str,
                             role_name: str) -> dict:
        """Change the role an enrolled name holds — a recorded
        act carrying the before and after. Assigning the role the
        name already holds changes nothing and is refused."""
        self._directory_actor(actor, "role assignment", name)
        entry = self._directory_target(actor, "role assignment",
                                       name)
        if self.get_role(role_name) is None:
            raise RoleError(
                "role_not_found",
                f"there is no role named '{role_name}' to assign")
        if entry["role"] == role_name:
            self._directory_refused(
                actor, "role assignment", entry["id"],
                "no_change",
                f"'{entry['id']}' already holds the role "
                f"'{role_name}'; the assignment changes nothing")
        updated = {**entry, "role": role_name}
        self.store.upsert_record("operators", updated)
        self._audit(ROLE_AUDIT_SCOPE, actor,
                    "operator_role_changed", {
                        "operator": entry["id"],
                        "old_role": entry["role"],
                        "new_role": role_name})
        return updated

    def deactivate_operator(self, actor: str, name: str) -> dict:
        """Deactivate a name: it may perform no act from this
        moment, and its unfinished work surfaces as orphaned (see
        `orphaned_work`) rather than moving or vanishing. The one
        deactivation the shell refuses outright is the last active
        administrator's — a directory nobody can administer is a
        lockout, not a governance act."""
        self._directory_actor(actor, "deactivation", name)
        entry = self._directory_target(actor, "deactivation", name)
        if not self.entry_active(entry):
            self._directory_refused(
                actor, "deactivation", entry["id"],
                "already_inactive",
                f"'{entry['id']}' is already deactivated")
        role = self.get_role(entry["role"])
        if role is not None and role.can_administer:
            peers = [e for e in self.list_operators()
                     if e["id"] != entry["id"]
                     and self.entry_active(e)
                     and (self.get_role(e["role"]) is not None
                          and self.get_role(e["role"]).can_administer)]
            if not peers:
                self._directory_refused(
                    actor, "deactivation", entry["id"],
                    "last_administrator",
                    f"'{entry['id']}' is the directory's last "
                    f"active administrator; deactivating it would "
                    f"leave nobody able to administer the "
                    f"directory — enrol or assign another "
                    f"administrator first")
        updated = {**entry, "active": False}
        self.store.upsert_record("operators", updated)
        self._audit(ROLE_AUDIT_SCOPE, actor,
                    "operator_deactivated", {
                        "operator": entry["id"],
                        "role": entry["role"]})
        return updated

    def reactivate_operator(self, actor: str, name: str) -> dict:
        """Bring a deactivated name back: the same entry, the same
        role, its whole history still attached — reactivation is
        the only route back, and it is recorded like the
        deactivation was."""
        self._directory_actor(actor, "reactivation", name)
        entry = self._directory_target(actor, "reactivation", name)
        if self.entry_active(entry):
            self._directory_refused(
                actor, "reactivation", entry["id"],
                "already_active",
                f"'{entry['id']}' is already active")
        updated = {**entry, "active": True}
        self.store.upsert_record("operators", updated)
        self._audit(ROLE_AUDIT_SCOPE, actor,
                    "operator_reactivated", {
                        "operator": entry["id"],
                        "role": entry["role"]})
        return updated

    # -- the task library ---------------------------------------------
    # Templates are governed records like the roles: seeded from
    # the defaults in templates.py through the strict loader, and
    # changed only by publishing a new version — a recorded
    # supervisor act in the library's own audit scope. A version
    # is never edited and never deleted: a publish supersedes the
    # previous version, which stays readable and instantiable by
    # name and version, the way superseded plan versions do.

    def _ensure_templates(self) -> None:
        """Seed the library from the governed defaults when the
        store has none. Called on every template read, so the
        library in force is always the stored one."""
        if not self.store.list_records("templates"):
            for raw in DEFAULT_TEMPLATES:
                template = load_template(raw)  # the seed passes it
                self.store.upsert_record("templates", {
                    "id": template.record_id,
                    **template.model_dump(mode="json")})

    def list_templates(self) -> list[TaskTemplate]:
        self._ensure_templates()
        templates = []
        for raw in self.store.list_records("templates"):
            raw = {k: v for k, v in raw.items() if k != "id"}
            templates.append(load_template(raw))
        return sorted(templates, key=lambda t: (t.name, t.version))

    def get_template(self, name: str,
                     version: Optional[int] = None
                     ) -> Optional[TaskTemplate]:
        """One template version: the exact version asked for, or
        — version None — the current (highest) version of the
        name. Callers that act on the result name the resolved
        version in what they record; nothing here is silent."""
        versions = [t for t in self.list_templates()
                    if t.name == name]
        if not versions:
            return None
        if version is None:
            return versions[-1]
        return next((t for t in versions if t.version == version),
                    None)

    def template_audit(self) -> list[dict]:
        return self.store.list_audit(TEMPLATE_AUDIT_SCOPE)

    def _template_refused(self, actor: str, act: str, target: str,
                          code: str, reason: str) -> None:
        self._audit(TEMPLATE_AUDIT_SCOPE, actor, "template_refused", {
            "act": act, "target": target, "code": code,
            "reason": reason})
        raise GateError(f"{act} refused: {reason}")

    def _template_actor(self, actor: str, act: str,
                        target: str) -> dict:
        """The gate every library administration act passes: the
        acting name must be enrolled, active, and holding a role
        that may administer. Each refusal is recorded before it
        raises — the directory acts' discipline, in the library's
        scope."""
        entry = self.directory_entry(actor)
        if entry is None:
            self._template_refused(
                actor, act, target, "unknown_operator",
                f"'{actor}' holds no role in the operator "
                f"directory; publishing a template is a "
                f"supervisor act")
        if not self.entry_active(entry):
            self._template_refused(
                actor, act, target, "operator_inactive",
                f"'{actor}' is deactivated (operator_inactive) — "
                f"a deactivated operator performs no acts")
        role = self.get_role(entry["role"])
        if role is None or not role.can_administer:
            self._template_refused(
                actor, act, target, "role_cannot_administer",
                f"'{actor}' may not publish templates: the act "
                f"needs a role with can_administer (a supervisor)")
        return entry

    def publish_template(self, actor: str,
                         raw: dict) -> TaskTemplate:
        """Publish a new version of a template: a recorded
        supervisor act. The version number is the service's to
        assign — the next for the name — and the content must
        pass the strict loader whole. Publishing content identical
        to the current version changes nothing and is refused;
        the version it supersedes is kept, marked superseded,
        and stays instantiable by name and version."""
        name = raw.get("name", "")
        self._template_actor(actor, "template publish", name)
        current = self.get_template(name)
        candidate = load_template({
            **raw,
            "version": (current.version + 1) if current else 1,
            "published_by": actor, "published_at": _now(),
            "superseded": False})
        if current is not None and \
                _template_content(candidate) == \
                _template_content(current):
            self._template_refused(
                actor, "template publish", name, "no_change",
                f"the new version of '{name}' is identical to "
                f"v{current.version}; a publish that changes "
                f"nothing is not a version")
        if current is not None:
            current.superseded = True
            self.store.upsert_record("templates", {
                "id": current.record_id,
                **current.model_dump(mode="json")})
        self.store.upsert_record("templates", {
            "id": candidate.record_id,
            **candidate.model_dump(mode="json")})
        self._audit(TEMPLATE_AUDIT_SCOPE, actor,
                    "template_published", {
                        "template": name,
                        "version": candidate.version,
                        "supersedes": (current.version
                                       if current else None)})
        return candidate

    def _instantiation_refused(self, operator: str, name: str,
                               version: Optional[int], code: str,
                               reason: str) -> None:
        self._audit(TEMPLATE_AUDIT_SCOPE, operator,
                    "template_instantiation_refused", {
                        "template": name, "version": version,
                        "code": code, "reason": reason})

    def _validate_slots(self, template: TaskTemplate,
                        slots: dict[str, str]) -> dict[str, str]:
        """Validate slot values against the template's slot
        declarations, before any text is rendered: every declared
        slot filled, nothing undeclared offered, and each value
        fit for its kind — a record slot names a record that
        exists (the planner's own refusal, in its own words), a
        money slot parses as money, a date slot is an ISO calendar
        date. Raises TemplateError; instantiation records it."""
        declared = {s.name: s for s in template.slots}
        for name in slots:
            if name not in declared:
                raise TemplateError(
                    "slot_not_declared",
                    f"refused: '{name}' is not a slot of template "
                    f"'{template.name}' v{template.version}; its "
                    f"slots are {', '.join(sorted(declared))}")
        values: dict[str, str] = {}
        for slot in template.slots:
            if slot.name not in slots:
                raise TemplateError(
                    "slot_missing",
                    f"refused: template '{template.name}' "
                    f"v{template.version} needs a value for its "
                    f"'{slot.name}' slot; a template is never "
                    f"filled by guessing")
            value = slots[slot.name]
            if slot.kind == "record":
                if self.store.get_record(slot.record_kind or "",
                                         value) is None:
                    raise TemplateError(
                        "record_not_found",
                        f"refused: {(slot.record_kind or '')[:-1]} "
                        f"{value} does not exist; a plan is only "
                        f"derived from records that are there "
                        f"(slot '{slot.name}')")
            elif slot.kind == "money":
                try:
                    money(value)
                except (InvalidOperation, ValueError):
                    raise TemplateError(
                        "slot_not_money",
                        f"refused: '{value}' for slot "
                        f"'{slot.name}' is not an amount")
            elif slot.kind == "date":
                try:
                    datetime.strptime(value, "%Y-%m-%d")
                    valid = len(value) == 10
                except ValueError:
                    valid = False
                if not valid:
                    raise TemplateError(
                        "slot_not_a_date",
                        f"refused: '{value}' for slot "
                        f"'{slot.name}' is not a date "
                        f"(YYYY-MM-DD)")
            values[slot.name] = value
        return values

    def instantiate_template(self, name: str,
                             version: Optional[int],
                             slots: dict[str, str],
                             operator: str) -> Task:
        """Instantiate a template: validate the slots, render the
        task text, and hand it to intake — the ordinary door, so
        the planner derives the plan from the rendered text and
        the role rules and policy apply exactly as they do to a
        typed task. A slot value that renders text the planner
        cannot derive, or policy refuses, returns the REFUSED
        task intake itself produces, with the planner's reason on
        it: instantiation adds no bypass and no second refusal
        language. The task records the template's name and the
        exact version used — the resolved version, named even
        when the caller asked for the current one — and the act
        is written to the task's audit trail."""
        template = self.get_template(name, version)
        if template is None:
            which = (f"'{name}' v{version}" if version is not None
                     else f"'{name}'")
            reason = (f"there is no template {which} in the "
                      f"library")
            self._instantiation_refused(operator, name, version,
                                        "template_not_found", reason)
            raise TemplateError("template_not_found",
                                f"refused: {reason}")
        try:
            values = self._validate_slots(template, slots)
        except TemplateError as err:
            self._instantiation_refused(
                operator, name, template.version, err.code,
                err.message)
            raise
        task = self.intake(render_pattern(template.pattern, values),
                           operator)
        task.template_name = template.name
        task.template_version = template.version
        self.store.put_task(task.model_dump(mode="json"))
        self._audit(task.task_id, operator, "template_instantiated", {
            "template": template.name, "version": template.version,
            "requested_version": version, "slots": values})
        return task

    def template_health(self, template: TaskTemplate) -> dict:
        """A version's health, computed on read from its pinned
        examples: each example's rendered text is derived against
        a scratch store seeded with the OrderDesk records — the
        same basis the eval gate grades on — and the derived plan
        must be the shape the example pins. The repo's eval
        runner stores no results, so there is no stored verdict
        to read back; this is the examples, re-derived at the
        moment of asking."""
        from .dataset import seed_records

        scratch = InMemoryStore()
        scratch.seed_if_empty(seed_records())
        examples = []
        for example in template.examples:
            try:
                plan = derive_plan(
                    render_pattern(template.pattern, example.slots),
                    scratch, self.settings.policy, "TASK-HEALTH")
                detail = shape_detail(plan, example)
            except PlanRefusal as refusal:
                detail = (f"the rendered text is refused: "
                          f"{refusal.code}: {refusal.message}")
            examples.append({
                "slots": dict(example.slots),
                "ok": detail is None,
                "detail": detail or ""})
        return {"ok": all(e["ok"] for e in examples),
                "examples": examples}

    def template_view(self, template: TaskTemplate) -> dict:
        """One version as the library serves it: the record, with
        its health folded in and `current` naming whether it is
        the version a name-only instantiation would use."""
        view = template.model_dump(mode="json")
        view["current"] = not template.superseded
        view["health"] = self.template_health(template)
        return view

    def list_template_views(self) -> list[dict]:
        return [self.template_view(t) for t in self.list_templates()]

    # -- intake + planning ---------------------------------------------
    def intake(self, text: str, operator: str) -> Task:
        task_id = f"TASK-{uuid.uuid4().hex[:8].upper()}"
        task = Task(task_id=task_id, text=text, operator=operator,
                    planned_by=operator,
                    created_at=_now(), updated_at=_now())
        self.store.put_task(task.model_dump(mode="json"))
        self._audit(task_id, operator, "task_created", {"text": text})
        try:
            # The planner's identity is checked before the plan
            # exists: a name with no directory entry, a deactivated
            # name, or a role that may not plan is refused here —
            # at plan time, recorded like any other plan refusal.
            entry = self.directory_entry(operator)
            role = self.role_of(operator)
            if entry is None or role is None:
                raise PlanRefusal(
                    "unknown_operator",
                    f"refused: '{operator}' holds no role in the "
                    f"operator directory; an unknown operator may "
                    f"not plan work")
            if not self.entry_active(entry):
                raise PlanRefusal(
                    "operator_inactive",
                    f"refused: '{operator}' is deactivated "
                    f"(operator_inactive); a deactivated operator "
                    f"may not plan work — the name returns by a "
                    f"supervisor's reactivation, not by acting")
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
        entry = self.directory_entry(operator)
        if entry is not None and not self.entry_active(entry):
            self._audit(task_id, operator, "amendment_refused", {
                "code": "operator_inactive",
                "reason": f"'{operator}' is deactivated; a "
                          f"deactivated operator may not "
                          f"amend plans"})
            raise GateError(
                f"amendment to task {task_id} refused: "
                f"'{operator}' is deactivated (operator_inactive)")
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

    def _require_active_actor(self, task_id: str, operator: str,
                              act: str) -> None:
        """Fail closed on a deactivated actor, for the acts whose
        refusals this state machine has never recorded (reject,
        re-plan): a deactivated name performs no act, full stop."""
        entry = self.directory_entry(operator)
        if entry is not None and not self.entry_active(entry):
            raise GateError(
                f"{act} of task {task_id} refused: '{operator}' "
                f"is deactivated (operator_inactive) — a "
                f"deactivated operator performs no acts")

    def reject(self, task_id: str, operator: str) -> Task:
        """A human's no to a planned or confirmed task. Terminal:
        the operator states a fresh task if the work is still
        wanted, so no rejected plan can be revived into execution."""
        task = self._require_task(task_id)
        self._require_active_actor(task_id, operator, "rejection")
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
        self._require_active_actor(task_id, operator, "re-plan")
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
        # planner). Every refusal is recorded before it raises —
        # including the deactivated confirmer's: a name that may
        # perform no act is refused as itself, not as a stranger.
        entry = self.directory_entry(operator)
        if entry is not None and not self.entry_active(entry):
            self._audit(task_id, operator, "confirmation_refused", {
                "code": "operator_inactive",
                "reason": f"'{operator}' is deactivated; a "
                          f"deactivated operator may not "
                          f"confirm plans",
                "planner": task.operator,
                "plan_version": task.plan_version})
            raise GateError(
                f"confirmation of task {task_id} refused: "
                f"'{operator}' is deactivated (operator_inactive)")
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

    # -- execution attempts ----------------------------------------------
    # Attempts are stored records (kind `executions`, keyed
    # "<task_id>:<number>") — the same records mechanism the roles
    # and the verification reports ride. An attempt's outcomes are
    # the execution's own account of itself; the audit trail
    # carries the same steps as events, as it always has.

    @staticmethod
    def _attempt_key(task_id: str, number: int) -> str:
        return f"{task_id}:{number}"

    def _store_attempt(self, attempt: ExecutionAttempt) -> None:
        self.store.upsert_record("executions", {
            "id": self._attempt_key(attempt.task_id, attempt.number),
            **attempt.model_dump(mode="json")})

    def get_attempts(self, task_id: str) -> list[ExecutionAttempt]:
        """Every attempt this task has made, in number order —
        each with the steps it completed and, for a failed attempt,
        the step it stopped at and the driver's error verbatim."""
        attempts = []
        for raw in self.store.list_records("executions"):
            raw = {k: v for k, v in raw.items() if k != "id"}
            attempt = ExecutionAttempt(**raw)
            if attempt.task_id == task_id:
                attempts.append(attempt)
        return sorted(attempts, key=lambda a: a.number)

    def _running_attempt(
            self, task_id: str) -> Optional[ExecutionAttempt]:
        return next((a for a in self.get_attempts(task_id)
                     if a.outcome == "running"), None)

    def _claim_attempt(self, task: Task, plan: Plan, driver: str,
                       mode: str, base_attempt: Optional[int],
                       actor: str) -> ExecutionAttempt:
        """Start a numbered attempt — the task's execution claim.

        The check-and-create runs under the service lock, so two
        callers cannot both claim: whoever arrives second finds
        the running attempt and is refused, recorded, before
        anything runs. `base_attempt` is the attempt the new one
        counts completed steps from — 1 for an execute or a resume
        (earlier work stands), and None, meaning the new attempt's
        own number, for a restart (the plan runs again from
        step 1).
        """
        with self._lock:
            running = self._running_attempt(task.task_id)
            if running is not None:
                self._audit(task.task_id, actor,
                            "execution_refused", {
                                "code": "attempt_in_flight",
                                "attempt": running.number,
                                "driver": running.driver,
                                "reason": f"attempt {running.number} "
                                          f"is still running",
                                "plan_version": plan.version})
                raise GateError(
                    f"execution of task {task.task_id} refused: "
                    f"attempt {running.number} (driver "
                    f"'{running.driver}') is still in flight — a "
                    f"task runs one attempt at a time")
            number = len(self.get_attempts(task.task_id)) + 1
            attempt = ExecutionAttempt(
                task_id=task.task_id, number=number, driver=driver,
                executor=actor,
                plan_version=plan.version, mode=mode,
                base_attempt=(number if base_attempt is None
                              else base_attempt),
                started_at=_now())
            self._store_attempt(attempt)
            task.attempts = number
            task.failed_step = None
            task.failure_error = None
            task.driver = driver
            self._save(task)
            return attempt

    def _completed_seqs(self, task_id: str,
                        base_attempt: int) -> set[int]:
        """The plan steps this execution counts as completed: the
        ok outcomes of every attempt from `base_attempt` on."""
        done: set[int] = set()
        for attempt in self.get_attempts(task_id):
            if attempt.number < base_attempt:
                continue
            done |= {o.seq for o in attempt.steps if o.ok}
        return done

    def _latest_outcomes(self, task_id: str) -> dict[int, StepOutcome]:
        """The standing outcome per plan step across all attempts:
        the latest completed outcome for each step. A failed
        outcome never displaces a completed one — the step did not
        land, whatever a later attempt tried."""
        latest: dict[int, StepOutcome] = {}
        for attempt in self.get_attempts(task_id):
            for outcome in attempt.steps:
                if outcome.ok:
                    latest[outcome.seq] = outcome
        return latest

    def _created_ids(self, task_id: str, plan: Plan,
                     base_attempt: int) -> dict[int, str]:
        """The records the plan's CREATE steps created, by step
        seq, gathered from every attempt this execution counts —
        so a resumed execution verifies the memo its first attempt
        issued, not none. The offline driver's outcomes name the
        created id; a live attempt's memo is found by the task
        that created it, on its invoice."""
        created: dict[int, str] = {}
        create_steps = {s.seq: s for s in plan.steps
                        if s.action == StepAction.CREATE}
        for attempt in self.get_attempts(task_id):
            if attempt.number < base_attempt:
                continue
            for outcome in attempt.steps:
                if (outcome.ok and outcome.seq in create_steps
                        and outcome.after.get("id")):
                    created[outcome.seq] = str(outcome.after["id"])
        for seq, step in create_steps.items():
            if seq in created:
                continue
            # The memo the bridge issued for this task is the
            # step's product — prefer the one naming the planned
            # invoice, but take the task's memo even when it names
            # another: verification grades its fields and names
            # the difference, which it cannot do with no memo.
            memos = self.reads.memos_for_task(task_id)
            match = next(
                (m for m in memos
                 if str(m.get("invoice_id")) ==
                 step.fields.get("invoice_id")),
                memos[0] if memos else None)
            if match is not None:
                created[seq] = str(match["id"])
        return created

    @staticmethod
    def _state_lines(plan: Plan,
                     outcomes: list[StepOutcome]) -> list[str]:
        """Name, line by line, the state a set of completed step
        outcomes left in place — the partial state of a failed
        attempt, stated plainly enough to stand in the attempt
        record, in the audit trail, and in an abandonment."""
        steps = {s.seq: s for s in plan.steps}
        lines = []
        for outcome in outcomes:
            if not outcome.ok:
                continue
            step = steps[outcome.seq]
            if step.action == StepAction.SET:
                lines.append(
                    f"step {step.seq}: {step.record_kind} "
                    f"{step.record_id}.{step.field} is now "
                    f"'{outcome.after.get(step.field, '')}'")
            else:
                lines.append(
                    f"step {step.seq}: created "
                    f"{step.record_kind[:-1]} "
                    f"{outcome.after.get('id', '')} against invoice "
                    f"{step.fields.get('invoice_id', '')}")
        return lines

    def _fail_execution(self, task: Task, plan: Plan,
                        attempt: ExecutionAttempt,
                        failed: StepOutcome) -> Task:
        """Finalize a failed attempt. The attempt record closes
        with the failed step and the driver's error verbatim, and
        the task lands in EXECUTION_FAILED carrying the same two
        facts. Nothing is rolled back and nothing is cleaned up:
        the completed steps stand exactly as they landed, named in
        the attempt's state lines and in the audit entry."""
        attempt.outcome = "failed"
        attempt.finished_at = _now()
        attempt.failed_seq = failed.seq
        attempt.error = failed.detail
        attempt.state_lines = self._state_lines(plan, attempt.steps)
        self._store_attempt(attempt)
        self._audit(task.task_id, attempt.driver, "step_failed", {
            "seq": failed.seq, "attempt": attempt.number,
            "mode": attempt.mode, "plan_version": plan.version,
            "error": failed.detail, "before": failed.before})
        task.status = TaskStatus.EXECUTION_FAILED
        task.failed_step = failed.seq
        task.failure_error = failed.detail
        self._save(task)
        self._audit(task.task_id, attempt.driver, "execution_failed", {
            "attempt": attempt.number, "mode": attempt.mode,
            "driver": attempt.driver, "executor": attempt.executor,
            "plan_version": plan.version,
            "failed_seq": failed.seq, "error": failed.detail,
            "completed_steps": [o.seq for o in attempt.steps if o.ok],
            "partial_state": attempt.state_lines})
        return task

    def _complete_attempt(self, task: Task, plan: Plan,
                          attempt: ExecutionAttempt) -> Task:
        """Finalize a completed attempt and run the verification
        act over the whole plan — the created records gathered
        from every attempt this execution counts, so the verdict
        covers the plan, not just the final attempt's steps."""
        attempt.outcome = "completed"
        attempt.finished_at = _now()
        attempt.state_lines = self._state_lines(plan, attempt.steps)
        self._store_attempt(attempt)
        self._audit(task.task_id, attempt.driver,
                    "execution_completed", {
                        "driver": attempt.driver,
                        "executor": attempt.executor,
                        "attempt": attempt.number,
                        "mode": attempt.mode,
                        "steps": len(plan.steps)})
        return self._verify(
            task, plan,
            self._created_ids(task.task_id, plan,
                              attempt.base_attempt))

    def _run_offline_steps(self, task: Task, plan: Plan,
                           attempt: ExecutionAttempt,
                           steps: list[PlanStep],
                           tamper: Optional[dict[int, str]],
                           fail: Optional[dict[int, str]]) -> Task:
        """Run plan steps through the offline driver for one
        attempt. A step that fails — the driver's failure outcome,
        or an error the driver raises — stops the execution there:
        the attempt and the task record the failure, and no later
        step runs. A step's error is data for the record, never an
        exception that strands the task mid-plan with nothing
        written down."""
        runner = OfflineDriver(tamper=tamper, fail=fail)
        for step in steps:
            try:
                outcome = runner.run_step(self.store, task, plan, step)
            except Exception as exc:  # the driver's error, verbatim
                outcome = StepOutcome(
                    seq=step.seq, driver=DRIVER_OFFLINE, ok=False,
                    detail=str(exc))
            attempt.steps.append(outcome)
            self._store_attempt(attempt)
            if not outcome.ok:
                return self._fail_execution(task, plan, attempt,
                                            outcome)
            self._audit(task.task_id, DRIVER_OFFLINE,
                        "step_executed", {
                            "seq": step.seq,
                            "attempt": attempt.number,
                            "plan_version": plan.version,
                            "before": outcome.before,
                            "after": outcome.after,
                            "detail": outcome.detail})
        return self._complete_attempt(task, plan, attempt)

    # -- execution ---------------------------------------------------------
    def execute(self, task_id: str, driver: str = DRIVER_OFFLINE,
                *, tamper: Optional[dict[int, str]] = None,
                fail: Optional[dict[int, str]] = None,
                operator: Optional[str] = None) -> Task:
        task = self._require_task(task_id)
        if task.status != TaskStatus.CONFIRMED:
            raise GateError(
                f"task {task_id} is {task.status.value}; execution "
                f"requires a CONFIRMED plan — an unconfirmed plan "
                f"cannot execute")
        if driver not in (DRIVER_OFFLINE, DRIVER_LIVE):
            raise GateError(f"unknown driver '{driver}'")
        plan = self._bound_plan(task)
        # Permissions are re-checked at execution, against the role
        # definitions in force NOW: the executor must hold an
        # execute-capable role, and the bound plan must still fit
        # both the executor's allowlist and the planner's. A role
        # narrowed after confirmation therefore holds the task —
        # refused, recorded, still CONFIRMED, nothing run — until
        # the definitions allow the plan again.
        executor = operator if operator is not None else task.operator
        self._refuse_inactive_executor(task, plan, executor)
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
        # The attempt claim: one execution at a time, and a
        # re-execution of a task that already ran its course is
        # refused by the status check above — VERIFIED is final.
        attempt = self._claim_attempt(task, plan, driver, "execute",
                                      1, executor)
        if driver == DRIVER_LIVE:
            # The agent runs in the operator's browser; the bridge
            # reports each step back. The attempt stays running —
            # the claim — until the final report lands or a step
            # reports failure (see report_step).
            self._audit(task_id, task.operator, "execution_started", {
                "driver": driver, "plan_version": plan.version,
                "attempt": attempt.number, "steps": len(plan.steps)})
            return task
        return self._run_offline_steps(task, plan, attempt,
                                       list(plan.steps), tamper, fail)

    def report_step(self, task_id: str, seq: int,
                    report: StepReportIn) -> Task:
        """Record one live step report from the browser bridge.

        The report is advisory: it is written to the audit and to
        the running attempt, and when the execution's last
        outstanding step lands the shell verifies against the
        store — the report's own `ok` is never treated as
        evidence. A report with `ok` False is the live driver's
        step failure: the execution stops there, exactly as an
        offline failure does, and steps after it are never
        awaited."""
        task = self._require_task(task_id)
        if task.status != TaskStatus.CONFIRMED or task.driver != DRIVER_LIVE:
            raise GateError(
                f"task {task_id} is not awaiting live step reports "
                f"(status {task.status.value}, driver {task.driver})")
        plan = self._bound_plan(task)
        if seq not in {s.seq for s in plan.steps}:
            raise GateError(f"plan for task {task_id} has no step {seq}")
        attempt = self._running_attempt(task_id)
        if attempt is None:
            raise GateError(
                f"task {task_id} has no execution attempt in flight "
                f"to report against")
        if seq in {o.seq for o in attempt.steps}:
            raise GateError(
                f"step {seq} of task {task_id} was already reported")
        if seq in self._completed_seqs(task_id, attempt.base_attempt):
            raise GateError(
                f"step {seq} of task {task_id} was already completed "
                f"by an earlier attempt; a resumed execution runs "
                f"only the steps that never completed")
        detail = report.detail
        if not report.ok and not detail:
            detail = ("the in-page agent reported this step failed "
                      "and gave no detail")
        outcome = StepOutcome(seq=seq, driver=DRIVER_LIVE,
                              ok=report.ok, detail=detail)
        attempt.steps.append(outcome)
        self._store_attempt(attempt)
        self._audit(task_id, DRIVER_LIVE, "step_reported", {
            "seq": seq, "ok": report.ok, "detail": report.detail,
            "attempt": attempt.number, "plan_version": plan.version})
        if not report.ok:
            return self._fail_execution(task, plan, attempt, outcome)
        if self._completed_seqs(task_id, attempt.base_attempt) != \
                {s.seq for s in plan.steps}:
            return task
        return self._complete_attempt(task, plan, attempt)

    # -- failure recovery --------------------------------------------------
    def _refuse_inactive_executor(self, task: Task, plan: Plan,
                                  executor: str) -> None:
        """The identity half of the execution gate: an executor
        whose directory entry stands deactivated is refused as
        themselves — `operator_inactive`, recorded — before any
        allowlist is consulted. (The planner's side is deliberately
        not checked: a deactivated planner's confirmed work is
        orphaned work for a supervisor to pick up, and its
        allowlist check is about the plan, not the person.)"""
        entry = self.directory_entry(executor)
        if entry is not None and not self.entry_active(entry):
            self._audit(task.task_id, executor,
                        "execution_refused", {
                            "code": "operator_inactive",
                            "who": "executor",
                            "reason": f"'{executor}' is "
                                      f"deactivated; a deactivated "
                                      f"operator may not execute "
                                      f"plans",
                            "plan_version": plan.version})
            raise GateError(
                f"execution of task {task.task_id} refused: "
                f"'{executor}' is deactivated "
                f"(operator_inactive)")

    def _execution_gate(self, task: Task, plan: Plan,
                        executor: str) -> None:
        """The execution-time role re-check for the recovery acts,
        in the same shape `execute` runs it: the executor must hold
        an execute-capable role, and the bound plan must still fit
        both the planner's allowlist and the executor's, against
        the definitions in force now. A refusal is recorded before
        it raises; the task stays EXECUTION_FAILED."""
        self._refuse_inactive_executor(task, plan, executor)
        checks: list[tuple[str, Optional[RoleDefinition]]] = [
            ("planner", self.role_of(task.operator)),
            ("executor", self.role_of(executor)),
        ]
        for who, role in checks:
            if role is None:
                self._audit(task.task_id, executor,
                            "execution_refused", {
                                "code": "unknown_operator",
                                "who": who,
                                "reason": f"the {who} holds no role",
                                "plan_version": plan.version})
                raise GateError(
                    f"execution of task {task.task_id} refused: the "
                    f"{who} of this task holds no role in the "
                    f"operator directory")
            if who == "executor" and not role.can_execute:
                self._audit(task.task_id, executor,
                            "execution_refused", {
                                "code": "role_cannot_execute",
                                "who": who, "role": role.name,
                                "reason": f"role '{role.name}' may "
                                          f"not execute plans",
                                "plan_version": plan.version})
                raise GateError(
                    f"execution of task {task.task_id} refused: role "
                    f"'{role.name}' may not execute plans")
            problems = plan_problems(role, plan)
            if problems:
                first = problems[0]
                self._audit(task.task_id, executor,
                            "execution_refused", {
                                "code": first["problem"], "who": who,
                                "role": role.name,
                                "field": first["field"],
                                "record_kind": first["record_kind"],
                                "reason": describe_problem(role, first),
                                "plan_version": plan.version})
                raise GateError(
                    f"execution of task {task.task_id} refused: "
                    f"{describe_problem(role, first)} — the role "
                    f"definitions in force no longer allow the "
                    f"{who}'s plan; the task stays EXECUTION_FAILED")

    def _basis_problems(self, task: Task, plan: Plan) -> list[str]:
        """Why this task's failed execution may not be picked up
        again, decided in code from the records — an empty list
        means it may. Two checks:

        1. Every completed step's post-state still stands: a SET
           step's field still holds the value the step left; a
           CREATE step's record still exists, with its planned
           fields as created.
        2. The records the remaining steps rest on still hash to
           the plan's preconditions — except the records a
           completed step itself wrote, whose movement is the
           completed work and is covered by check 1.

        A plan is never re-run over state that moved underneath
        it: whoever moved it, the shell names it and stops."""
        completed = self._latest_outcomes(task.task_id)
        steps = {s.seq: s for s in plan.steps}
        problems: list[str] = []
        written: set[tuple[str, str]] = set()
        for seq in sorted(completed):
            outcome, step = completed[seq], steps[seq]
            if step.action == StepAction.SET:
                written.add((step.record_kind, step.record_id or ""))
                record = self.store.get_record(
                    step.record_kind, step.record_id or "")
                field = step.field or ""
                expected = outcome.after.get(field, step.value)
                actual = (str(record.get(field, ""))
                          if record is not None else None)
                if actual != expected:
                    problems.append(
                        f"step {seq}: {step.record_kind} "
                        f"{step.record_id}.{field} was left as "
                        f"'{expected}' by the completed step and now "
                        f"reads '{actual}'")
                continue
            record = None
            memo_id = outcome.after.get("id")
            if memo_id:
                record = self.store.get_record(
                    step.record_kind, str(memo_id))
            if record is None:
                for memo in self.reads.memos_for_task(task.task_id):
                    if str(memo.get("invoice_id")) == \
                            step.fields.get("invoice_id"):
                        record = memo
                        break
            if record is None:
                problems.append(
                    f"step {seq}: the {step.record_kind[:-1]} the "
                    f"completed step created is no longer there")
                continue
            written.add((step.record_kind, str(record["id"])))
            for field, planned in sorted(step.fields.items()):
                if str(record.get(field, "")) != planned:
                    problems.append(
                        f"step {seq}: created "
                        f"{step.record_kind[:-1]} {record['id']}."
                        f"{field} was '{planned}' as created and "
                        f"now reads '{record.get(field, '')}'")
        preconditions = {(p.record_kind, p.record_id): p.hash
                         for p in plan.preconditions}
        seen_records: set[tuple[str, str]] = set()
        for step in plan.steps:
            if step.seq in completed:
                continue
            for kind, record_id in step_targets(step):
                if (kind, record_id) in written or \
                        (kind, record_id) in seen_records:
                    continue
                seen_records.add((kind, record_id))
                record = self.store.get_record(kind, record_id)
                current = (record_hash(record) if record is not None
                           else "missing")
                if current != preconditions.get((kind, record_id)):
                    problems.append(
                        f"{kind} {record_id} changed since plan "
                        f"v{plan.version} was derived, and step "
                        f"{step.seq} rests on it")
        return problems

    def _refuse_recovery(self, task: Task, act: str, executor: str,
                         code: str, problems: list[str],
                         plan: Plan) -> None:
        self._audit(task.task_id, executor, f"{act}_refused", {
            "code": code, "problems": problems,
            "plan_version": plan.version})
        raise GateError(
            f"{act} of task {task.task_id} refused: "
            + "; ".join(problems)
            + " — the state the plan was executing against has "
              "moved; what stands is dealt with as it is, never "
              "written over")

    def resume_execution(self, task_id: str,
                         operator: Optional[str] = None,
                         *, tamper: Optional[dict[int, str]] = None,
                         fail: Optional[dict[int, str]] = None) -> Task:
        """Resume a failed execution: run only the steps that
        never completed, as a new attempt, under the failed
        attempt's driver.

        Decided in code before anything runs: the executor's role
        is re-checked (as at execution), every completed step's
        post-state must still stand, and the records the remaining
        steps rest on must still match the plan's preconditions.
        A basis that moved refuses the resume and names what
        moved — the resume never writes over a change it cannot
        account for. The resumed execution then verifies over the
        whole plan, the first attempt's work included."""
        task = self._require_task(task_id)
        if task.status != TaskStatus.EXECUTION_FAILED:
            raise GateError(
                f"task {task_id} is {task.status.value}; only a "
                f"failed execution can be resumed")
        plan = self._bound_plan(task)
        executor = operator if operator is not None else task.operator
        self._execution_gate(task, plan, executor)
        problems = self._basis_problems(task, plan)
        if problems:
            self._refuse_recovery(task, "resume", executor,
                                  "basis_moved", problems, plan)
        attempts = self.get_attempts(task_id)
        driver = (attempts[-1].driver if attempts
                  else task.driver or DRIVER_OFFLINE)
        done = self._completed_seqs(task_id, 1)
        remaining = [s for s in plan.steps if s.seq not in done]
        attempt = self._claim_attempt(task, plan, driver, "resume",
                                      1, executor)
        self._audit(task_id, executor, "execution_started", {
            "driver": driver, "attempt": attempt.number,
            "mode": "resume", "plan_version": plan.version,
            "steps": [s.seq for s in remaining]})
        task.status = TaskStatus.CONFIRMED
        self._save(task)
        if driver == DRIVER_LIVE:
            # Re-armed: the bridge reports the remaining steps;
            # the ones already completed stand from the attempts
            # before and are not reported again.
            return task
        return self._run_offline_steps(task, plan, attempt,
                                       remaining, tamper, fail)

    def restart_execution(self, task_id: str,
                          operator: Optional[str] = None,
                          *, tamper: Optional[dict[int, str]] = None,
                          fail: Optional[dict[int, str]] = None) -> Task:
        """Restart a failed execution from step 1, as a new
        attempt — allowed only when no step's writes would
        double-apply. A SET step re-applies its own value, which
        changes nothing; a CREATE step is never run twice, so a
        plan whose create step already completed can only be
        resumed, and the restart refuses and says so. The same
        basis checks as a resume run first: the state must still
        be exactly the plan's preconditions plus the completed
        steps' writes."""
        task = self._require_task(task_id)
        if task.status != TaskStatus.EXECUTION_FAILED:
            raise GateError(
                f"task {task_id} is {task.status.value}; only a "
                f"failed execution can be restarted")
        plan = self._bound_plan(task)
        executor = operator if operator is not None else task.operator
        self._execution_gate(task, plan, executor)
        steps = {s.seq: s for s in plan.steps}
        created = [seq for seq in sorted(self._latest_outcomes(task_id))
                   if steps[seq].action == StepAction.CREATE]
        if created:
            self._audit(task_id, executor, "restart_refused", {
                "code": "create_already_applied", "seq": created[0],
                "plan_version": plan.version})
            raise GateError(
                f"restart of task {task_id} refused: step "
                f"{created[0]} already created its record in an "
                f"earlier attempt, and a create is never run "
                f"twice — resume instead, which runs only the "
                f"steps that never completed")
        problems = self._basis_problems(task, plan)
        if problems:
            self._refuse_recovery(task, "restart", executor,
                                  "basis_moved", problems, plan)
        attempts = self.get_attempts(task_id)
        driver = (attempts[-1].driver if attempts
                  else task.driver or DRIVER_OFFLINE)
        attempt = self._claim_attempt(task, plan, driver, "restart",
                                      None, executor)
        self._audit(task_id, executor, "execution_started", {
            "driver": driver, "attempt": attempt.number,
            "mode": "restart", "plan_version": plan.version,
            "steps": [s.seq for s in plan.steps]})
        task.status = TaskStatus.CONFIRMED
        self._save(task)
        if driver == DRIVER_LIVE:
            return task
        return self._run_offline_steps(task, plan, attempt,
                                       list(plan.steps), tamper, fail)

    def abandon(self, task_id: str, operator: str,
                reason: str) -> Task:
        """Close a failed execution as ABANDONED: a recorded act,
        by an operator whose role may execute, with a mandatory
        reason. The partial state is not cleaned up — the writes
        the completed steps landed stay on the records, named in
        the final attempt's state lines and in this audit entry,
        and the task closes as ABANDONED, never as anything that
        reads like a success."""
        task = self._require_task(task_id)
        if task.status != TaskStatus.EXECUTION_FAILED:
            raise GateError(
                f"task {task_id} is {task.status.value}; only a "
                f"failed execution can be abandoned — a task that "
                f"never failed leaves by its own exits")
        if not (reason or "").strip():
            self._audit(task_id, operator, "abandon_refused", {
                "code": "reason_required",
                "reason": "abandoning a failed execution requires "
                          "a stated reason"})
            raise GateError(
                f"task {task_id} not abandoned: a reason is "
                f"required — the record names the partial state "
                f"and states why it stands")
        entry = self.directory_entry(operator)
        if entry is not None and not self.entry_active(entry):
            self._audit(task_id, operator, "abandon_refused", {
                "code": "operator_inactive",
                "reason": f"'{operator}' is deactivated; a "
                          f"deactivated operator may not abandon "
                          f"executions"})
            raise GateError(
                f"task {task_id} not abandoned: '{operator}' is "
                f"deactivated (operator_inactive)")
        role = self.role_of(operator)
        if role is None or not role.can_execute:
            why = (f"'{operator}' holds no role" if role is None
                   else f"role '{role.name}' may not execute plans")
            self._audit(task_id, operator, "abandon_refused", {
                "code": "role_cannot_abandon", "reason": why})
            raise GateError(
                f"task {task_id} not abandoned: {why}; abandoning "
                f"a failed execution is an execution act")
        attempts = self.get_attempts(task_id)
        last = attempts[-1] if attempts else None
        task.status = TaskStatus.ABANDONED
        task.abandoned_by = operator
        task.abandon_reason = reason.strip()
        self._save(task)
        self._audit(task_id, operator, "task_abandoned", {
            "reason": task.abandon_reason,
            "attempt": last.number if last else None,
            "failed_seq": task.failed_step,
            "error": task.failure_error,
            "partial_state": last.state_lines if last else []})
        return task

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
        entry = self.directory_entry(operator)
        if entry is not None and not self.entry_active(entry):
            self._audit(task_id, operator, "corrective_refused", {
                "code": "operator_inactive",
                "reason": f"'{operator}' is deactivated; a "
                          f"deactivated operator may not plan "
                          f"work, corrective or otherwise"})
            raise GateError(
                f"corrective task for {task_id} refused: "
                f"'{operator}' is deactivated (operator_inactive)")
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
            operator=operator, planned_by=operator,
            status=TaskStatus.PLANNED,
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
        entry = self.directory_entry(operator)
        if entry is not None and not self.entry_active(entry):
            self._audit(task_id, operator, "deviation_refused", {
                "code": "operator_inactive",
                "reason": f"'{operator}' is deactivated; a "
                          f"deactivated operator may not accept "
                          f"deviations"})
            raise GateError(
                f"deviation on task {task_id} not accepted: "
                f"'{operator}' is deactivated (operator_inactive)")
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

    # -- orphaned work and pickup --------------------------------------
    def orphaned_work(self) -> list[dict]:
        """The unfinished work of deactivated operators: every
        task whose operator stands deactivated while the task
        still owes that operator an act (ORPHANED_STATUSES).
        Nothing here is reassigned or deleted — the list is how
        the work is seen, and picking an item up is the separate
        recorded act below. A task whose confirmer was
        deactivated is not orphaned: a confirmation is a past
        act and stands; what strands work is its owner being
        unable to act on it."""
        orphaned: list[dict] = []
        for task in self.list_tasks():
            if task.status not in ORPHANED_STATUSES:
                continue
            entry = self.directory_entry(task.operator)
            if entry is None or self.entry_active(entry):
                continue
            orphaned.append({
                "task_id": task.task_id,
                "status": task.status.value,
                "operator": task.operator,
                "planned_by": task.planned_by or task.operator,
                "confirmed_by": task.confirmed_by,
                "plan_version": task.plan_version,
                "text": task.text,
            })
        return sorted(orphaned, key=lambda o: o["task_id"])

    def pickup_task(self, task_id: str, operator: str) -> Task:
        """Pick up a deactivated operator's orphaned task: a
        recorded supervisor act that re-names the task's operator
        to the supervisor. The original planner stays on the
        record (`planned_by`, and `picked_up_from` names whose
        work it was), and the audit entry carries both names.
        Picking up is the only way orphaned work moves — it is
        never auto-reassigned, and a task whose operator is
        active has nothing to pick up."""
        task = self._require_task(task_id)
        entry = self.directory_entry(operator)
        problem: Optional[tuple[str, str]] = None
        if entry is None:
            problem = ("unknown_operator",
                       f"'{operator}' holds no role in the "
                       f"operator directory")
        elif not self.entry_active(entry):
            problem = ("operator_inactive",
                       f"'{operator}' is deactivated; a "
                       f"deactivated operator may not pick up "
                       f"work")
        else:
            role = self.get_role(entry["role"])
            if role is None or not role.can_administer:
                problem = ("role_cannot_administer",
                           f"picking up orphaned work is a "
                           f"supervisor act (a role with "
                           f"can_administer)")
        if problem is None:
            owner = self.directory_entry(task.operator)
            if (owner is None or self.entry_active(owner)
                    or task.status not in ORPHANED_STATUSES):
                problem = ("not_orphaned",
                           f"task {task_id} is not orphaned "
                           f"work: its operator is not a "
                           f"deactivated name with work "
                           f"outstanding (status "
                           f"{task.status.value})")
        if problem is not None:
            code, reason = problem
            self._audit(task_id, operator, "pickup_refused", {
                "code": code, "reason": reason})
            raise GateError(
                f"pickup of task {task_id} refused: {reason}")
        previous = task.operator
        task.picked_up_from = previous
        task.operator = entry["id"]
        self._save(task)
        self._audit(task_id, operator, "task_picked_up", {
            "from_operator": previous,
            "to_operator": entry["id"],
            "planned_by": task.planned_by or previous,
            "status": task.status.value})
        return task

    # -- attribution -----------------------------------------------------
    def attribution_check(self) -> dict:
        """Scan the whole audit trail — every task scope and the
        roles scope — for an act that cannot be attributed: an
        entry whose actor is missing, or whose actor resolves
        neither to a directory name (in any state — a deactivated
        name's history still attributes) nor to one of the shell's
        named system components (SYSTEM_ACTORS). This is a check,
        not a repair: it names the entries and changes nothing.
        On a trail where every actor is known it reports zero
        problems, and the count of entries it scanned."""
        directory = {operator_named(e["id"])
                     for e in self.list_operators()}
        problems: list[dict] = []
        entries = self.store.list_all_audit()
        for entry in entries:
            actor = entry.get("actor") or ""
            base = {"scope": entry.get("task_id", ""),
                    "event": entry.get("event", ""),
                    "actor": actor}
            if not str(actor).strip():
                problems.append({**base, "problem": "actor_missing"})
            elif (operator_named(actor) not in directory
                    and operator_named(actor) not in SYSTEM_ACTORS):
                problems.append(
                    {**base, "problem": "actor_unresolvable"})
        return {"checked": len(entries), "ok": not problems,
                "problems": problems,
                "system_actors": sorted(SYSTEM_ACTORS)}

    def operator_report(self) -> list[dict]:
        """What each operator did, folded from the audit trail —
        never from counters, so the report cannot drift from the
        trail it summarises. Per directory name (active or
        deactivated): its acts by kind across every scope, the
        tasks it planned (its `task_created` acts), confirmed, and
        executed (the `execution_completed` entries naming it as
        executor), and the refusals it received, by code.

        Two attribution details are of their time: a plan refusal
        is written under the "policy" actor, so it is counted for
        the task's planner, whom the task record names; and
        executions completed before the directory round carry no
        executor on their entry, so those are counted for the
        task's operator — the executor `execute` defaulted to
        then."""
        entries = self.store.list_all_audit()
        tasks = {t.task_id: t for t in self.list_tasks()}
        report: list[dict] = []
        for raw in self.list_operators():
            name = operator_named(raw["id"])
            mine = [e for e in entries
                    if operator_named(e.get("actor") or "") == name]
            executed = 0
            for e in entries:
                if e.get("event") != "execution_completed":
                    continue
                executor = (e.get("detail") or {}).get("executor")
                if executor:
                    if operator_named(executor) == name:
                        executed += 1
                else:
                    task = tasks.get(e.get("task_id", ""))
                    if task is not None and operator_named(
                            task.operator) == name:
                        executed += 1
            refusals: Counter = Counter()
            for e in mine:
                if str(e.get("event", "")).endswith("_refused"):
                    refusals[(e.get("detail") or {}).get(
                        "code", "")] += 1
            for e in entries:
                if e.get("event") != "plan_refused":
                    continue
                task = tasks.get(e.get("task_id", ""))
                if task is None:
                    continue
                planner = task.planned_by or task.operator
                if operator_named(planner) == name:
                    refusals[(e.get("detail") or {}).get(
                        "code", "")] += 1
            report.append({
                "operator": raw["id"],
                "role": raw["role"],
                "active": self.entry_active(raw),
                "acts_total": len(mine),
                "acts_by_kind": dict(sorted(
                    Counter(e.get("event", "")
                            for e in mine).items())),
                "tasks_planned": sum(
                    1 for e in mine
                    if e.get("event") == "task_created"),
                "tasks_confirmed": sum(
                    1 for e in mine
                    if e.get("event") == "plan_confirmed"),
                "tasks_executed": executed,
                "refusals_received": sum(refusals.values()),
                "refusals_by_code": dict(sorted(refusals.items())),
            })
        return report
