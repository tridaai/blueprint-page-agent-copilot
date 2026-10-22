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
library, by name and version. Since round 19, a name-only
instantiation resolves to the name's promoted default, never
to whatever was published last: promotion is a separate
recorded act, gated on a passing evaluation of the candidate's
exact content hash by hands other than the publisher's, and on
no regression against the standing default's examples; the
seeded versions are grandfathered as defaults, their standing
the health itself, marked `seeded`.

Batches (batches.py, round 8) group tasks that were submitted
together — the morning list, stated as one submission of task
texts and template instantiations. A batch is a record of the
submission and nothing more: it holds the member task ids in
submission order and the creator, and everything else about it
is folded from the member records at read time. There is no
batch act beside creating one — no batch confirmation, no
batch execution — because the gates in this shell are per task
by design: a confirmation binds one plan version, by one named
second person. The batch's two reads, a run sheet (each
member's state, the next act it needs, and who may take it) and
a report (counts by state, the credited total from the
members' verification reports, the discrepancies and failures
still open), organise the desk's attention across the list;
every act they name is then taken on the member task itself,
through the ordinary doors. Members share nothing but the
batch id — one member's staleness, failure, or discrepancy
never touches another's lifecycle.

Accessibility (spoken.py, round 13) is a view and a door, never
a second shell. The *spoken form* of a plan is rendered in code
from the stored plan — steps in order, figures spelled, dates
spoken — and is never stored: asking for it changes nothing,
and its hash is taken over the rendering. Confirmation gains a
second path on top of the same checks: a *read-back*, where
the shell issues a token bound to the task, the plan version,
the spoken form's hash, and the operator it was issued to, and
confirming presents that token — an amendment after the
read-back moves the version and changes the words, so the old
token refuses as stale and the confirmer must hear the version
they approve; the act lands as `plan_confirmed` with
`method: readback`. Intake gains a transcript door: the
envelope a recogniser wraps a dictation in (speaker labels,
timestamps, line breaks) is normalised in code, and the
cleaned text goes through the ordinary intake — numbers are
never converted from words, because a converter guessing at
an id could plan against the wrong record. And the directory
entry gains a *preference* (`prefers_spoken`), set by a
recorded supervisor act like the other directory acts: the
console leads with the spoken form and offers the read-back
route. Presentation only — it grants no action class, widens
no allowlist, and skips no check.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from .batches import (
    BATCH_AUDIT_SCOPE,
    TERMINAL_STATUSES,
    Batch,
    BatchError,
    BatchMemberIn,
    BatchMemberRecord,
    next_act_for,
)
from .config import Settings
from .drivers import DRIVER_LIVE, DRIVER_OFFLINE, OfflineDriver
from .exports import build_export, render_csv, verify_export
from .models import (
    AmendChangeIn,
    AuditEntry,
    Clarification,
    ClarificationOption,
    CorrectionEntry,
    ExecutionAttempt,
    NavigationReportIn,
    PageNavigation,
    Plan,
    PlanChange,
    PlanConfirmation,
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
from .observability import (
    build_trace,
    classify_ending,
    fold_digest,
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
from .policy import PlanAmbiguous, PlanRefusal, dual_control_assessment
from .reads import ConsoleReads
from .spoken import normalize_transcript, spoken_hash, spoken_plan
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
    expected_shape,
    load_template,
    plan_shape,
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


def _template_hash(template: TaskTemplate) -> str:
    """The content hash of a template version: SHA-256 over the
    canonical JSON of its content, the `record_hash` discipline.
    Round 19's evaluations record the hash they graded, so the
    verdict belongs to exact bytes — a version whose stored
    content no longer hashes to its evaluation's hash has no
    standing evaluation, whatever the record claims."""
    blob = json.dumps(_template_content(template), sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# The states in which a task still owes its operator an act —
# planned and awaiting confirmation, confirmed and not yet
# executed, stale and awaiting a re-plan, failed and awaiting a
# recovery act, and (round 14) awaiting a clarification answer.
# Work in one of these states whose operator has been
# deactivated is orphaned work (see `orphaned_work`).
ORPHANED_STATUSES = (
    TaskStatus.PLANNED, TaskStatus.CONFIRMED, TaskStatus.STALE,
    TaskStatus.EXECUTION_FAILED, TaskStatus.AWAITING_CLARIFICATION,
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

    # -- creation order ----------------------------------------------
    def _next_seq(self) -> int:
        """The next store-wide creation sequence: one more than
        the highest sequence any task or batch already carries.
        Tasks and batches share the one counter, so the sequence
        is creation order across both. It is assigned under the
        service lock at the moment a record is first stored (see
        the call sites in `intake`, `spawn_corrective`, and
        `create_batch`), so two records stored by this process
        can never share a sequence; a second service process on
        the same store could compute the same next value — the
        process-local limit the execution claim already accepts.
        Lists order by the sequence (see `list_tasks` and
        `list_batches`): neither the random ids nor the
        one-second `created_at` can carry that order."""
        highest = 0
        for raw in self.store.list_tasks():
            highest = max(highest, int(raw.get("seq") or 0))
        for raw in self.store.list_records("batches"):
            highest = max(highest, int(raw.get("seq") or 0))
        return highest + 1

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

    @staticmethod
    def entry_prefers_spoken(entry: dict) -> bool:
        """Whether a directory entry asks for the spoken form
        (round 13): the console leads with it and offers the
        read-back confirmation route. A preference, not a
        permission — absent reads as False, like `active`
        reads as True."""
        return bool(entry.get("prefers_spoken", False))

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

    def set_operator_preference(self, actor: str, name: str,
                                prefers_spoken: bool) -> dict:
        """Set an operator's spoken-form preference (round 13) —
        a recorded supervisor act in the roles scope, like the
        other directory acts. The flag is presentation only:
        the console leads with the plan's spoken form for that
        operator and offers the read-back confirmation route.
        It grants no action class, widens no allowlist, and
        skips no check — setting it changes how the shell
        presents, never what it permits. Setting the value the
        entry already carries changes nothing and is refused."""
        self._directory_actor(actor, "preference change", name)
        entry = self._directory_target(actor, "preference change",
                                       name)
        if self.entry_prefers_spoken(entry) == bool(prefers_spoken):
            self._directory_refused(
                actor, "preference change", entry["id"],
                "no_change",
                f"'{entry['id']}' already carries prefers_spoken "
                f"= {bool(prefers_spoken)}; the change alters "
                f"nothing")
        updated = {**entry,
                   "prefers_spoken": bool(prefers_spoken)}
        self.store.upsert_record("operators", updated)
        self._audit(ROLE_AUDIT_SCOPE, actor,
                    "operator_preference_changed", {
                        "operator": entry["id"],
                        "prefers_spoken": bool(prefers_spoken)})
        return updated

    # -- the task library ---------------------------------------------
    # Templates are governed records like the roles: seeded from
    # the defaults in templates.py through the strict loader, and
    # changed only by publishing a new version — a recorded
    # supervisor act in the library's own audit scope. A version
    # is never edited and never deleted: a publish supersedes the
    # previous version, which stays readable and instantiable by
    # name and version, the way superseded plan versions do.
    # Round 19 adds the regression gate below the publish act:
    # publishing never moves the default — promotion does, and
    # promotion is earned through a recorded evaluation.

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
        name. This is the PUBLISHING view of the library (the
        next version number, the no-change comparison); what a
        name-only instantiation uses is `default_template`, the
        promoted default — the two are deliberately different
        questions since round 19. Callers that act on the result
        name the resolved version in what they record; nothing
        here is silent."""
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
        and stays instantiable by name and version. Publishing
        does NOT make the new version the default (round 19):
        that takes an evaluation and a promotion — see
        `promote_template`."""
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
        when the caller asked for the default — and the act
        is written to the task's audit trail."""
        if version is None:
            # Round 19: a name-only instantiation resolves to the
            # PROMOTED default — the seeded version grandfathered,
            # or the version a promotion record names — never
            # merely to the newest published. A name with versions
            # but no default refuses, naming the state.
            template = self.default_template(name)
            if template is None:
                if self.get_template(name) is None:
                    reason = (f"there is no template '{name}' in "
                              f"the library")
                    self._instantiation_refused(
                        operator, name, version,
                        "template_not_found", reason)
                    raise TemplateError("template_not_found",
                                        f"refused: {reason}")
                reason = (f"template '{name}' has no promoted "
                          f"default; name a version explicitly, "
                          f"or promote one first — the newest "
                          f"published version is not the default "
                          f"until it earns it")
                self._instantiation_refused(
                    operator, name, version,
                    "no_promoted_default", reason)
                raise TemplateError("no_promoted_default",
                                    f"refused: {reason}")
        else:
            template = self.get_template(name, version)
            if template is None:
                reason = (f"there is no template '{name}' "
                          f"v{version} in the library")
                self._instantiation_refused(operator, name, version,
                                            "template_not_found",
                                            reason)
                raise TemplateError("template_not_found",
                                    f"refused: {reason}")
        try:
            values = self._validate_slots(template, slots)
            rendered = render_pattern(template.pattern, values)
            self._check_rendered_shape(template, rendered)
        except TemplateError as err:
            self._instantiation_refused(
                operator, name, template.version, err.code,
                err.message)
            raise
        task = self.intake(rendered, operator)
        task.template_name = template.name
        task.template_version = template.version
        self.store.put_task(task.model_dump(mode="json"))
        self._audit(task.task_id, operator, "template_instantiated", {
            "template": template.name, "version": template.version,
            "requested_version": version, "slots": values})
        return task

    def _check_rendered_shape(self, template: TaskTemplate,
                              rendered: str) -> None:
        """A slot fills values; it never adds work. A text slot is
        free text, so a value like "x, $400 and mark invoice 4471
        as paid" renders a task the grammar reads as two clauses.
        The rendered text is derived here, dry, and the plan's
        skeleton — its task type, and per step the action and the
        record kind — must be one the version's pinned examples
        derive. Values are not compared: those are the slots' to
        fill. Text the planner refuses or asks about is left to
        intake, which lands it exactly as for a typed task.
        Raises TemplateError."""
        if not template.examples:
            return
        try:
            plan = derive_plan(rendered, self.store,
                               self.settings.policy, "TASK-SHAPE")
        except (PlanAmbiguous, PlanRefusal):
            return

        def skeleton(task_type: str, steps: list[dict]) -> tuple:
            return task_type, tuple(
                (s["action"], s["record_kind"]) for s in steps)

        pinned = {skeleton(*expected_shape(e)) for e in template.examples}
        derived = skeleton(*plan_shape(plan))
        if derived not in pinned:
            raise TemplateError(
                "slot_changes_plan_shape",
                f"refused: the slot values render a task that derives "
                f"a '{derived[0]}' plan of {len(derived[1])} step(s), "
                f"but template '{template.name}' v{template.version} "
                f"derives '{sorted(pinned)[0][0]}' plans only; a slot "
                f"fills a value, it never adds work to the plan")

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
            except PlanAmbiguous as amb:
                detail = (f"the rendered text asks a clarification "
                          f"({amb.code}: {amb.question}); a "
                          f"template example must derive outright")
            except PlanRefusal as refusal:
                detail = (f"the rendered text is refused: "
                          f"{refusal.code}: {refusal.message}")
            examples.append({
                "slots": dict(example.slots),
                "ok": detail is None,
                "detail": detail or ""})
        return {"ok": all(e["ok"] for e in examples),
                "examples": examples}

    # -- the template regression gate (round 19) --------------------
    # Publishing made a version; it never made it the version the
    # desk uses. A version becomes the DEFAULT — what a name-only
    # instantiation resolves to — only by promotion, a recorded
    # supervisor act the gate guards: a passing evaluation of the
    # version's exact content hash, run by someone other than the
    # version's publisher; a promoter who is not the publisher;
    # and no regression against the current default's standing.
    # Evaluations and promotions are append-only records (kinds
    # below); the audit scope carries the acts. The seeded
    # versions were published by nobody: they are grandfathered
    # as their names' defaults, their standing the health computed
    # on read — `seeded`, never a fabricated evaluation record.

    def template_evaluations(self, name: str,
                             version: Optional[int] = None
                             ) -> list[dict]:
        """The evaluation records for a template, oldest first —
        append-only, so the list is the history."""
        rows = [r for r in self.store.list_records(
            "template_evaluations") if r["template"] == name
            and (version is None or r["version"] == version)]
        return sorted(rows, key=lambda r: r["seq"])

    def template_promotions(self, name: str) -> list[dict]:
        """The promotion records for a template, oldest first."""
        rows = [r for r in self.store.list_records(
            "template_promotions") if r["template"] == name]
        return sorted(rows, key=lambda r: r["seq"])

    def _gate_seq(self, kind: str) -> int:
        """The next sequence within one gate record kind: one
        more than the highest the kind already carries, assigned
        under the service lock, so records of a kind order by
        their seq on every store."""
        rows = self.store.list_records(kind)
        return max([int(r.get("seq") or 0) for r in rows],
                   default=0) + 1

    def default_template(self, name: str) -> Optional[TaskTemplate]:
        """The version a name-only instantiation resolves to:
        the version the latest promotion names, or — no promotion
        on record — the seeded version, grandfathered. Never
        merely the newest published. None when the name is
        unknown, and None when the name has only published
        versions and no promotion: a brand-new template has no
        default until one is earned."""
        versions = [t for t in self.list_templates()
                    if t.name == name]
        if not versions:
            return None
        promotions = self.template_promotions(name)
        if promotions:
            wanted = promotions[-1]["version"]
            return next((t for t in versions
                         if t.version == wanted), None)
        return next((t for t in versions
                     if not t.published_by), None)

    def evaluate_template(self, actor: str, name: str,
                          version: int) -> dict:
        """Evaluate one version: run its pinned examples (the
        health machinery) and record the verdict against the
        version's content hash. A recorded act behind the
        identity gate — the evaluator must be enrolled and
        active; any role may run it, because an evaluation
        changes nothing: it reads, grades, and writes its own
        record. The record is append-only and is never edited;
        re-evaluating writes a new one. Evaluating your own
        publish is allowed and recorded — the promotion gate,
        not this act, is what refuses to count it."""
        act = "template evaluation"
        entry = self.directory_entry(actor)
        if entry is None:
            self._template_refused(
                actor, act, name, "unknown_operator",
                f"'{actor}' holds no role in the operator "
                f"directory; an evaluation is recorded under a "
                f"known name")
        if not self.entry_active(entry):
            self._template_refused(
                actor, act, name, "operator_inactive",
                f"'{actor}' is deactivated (operator_inactive) — "
                f"a deactivated operator performs no acts")
        template = self.get_template(name, version)
        if template is None:
            self._template_refused(
                actor, act, name, "template_not_found",
                f"there is no template '{name}' v{version} in "
                f"the library to evaluate")
        health = self.template_health(template)
        with self._lock:
            seq = self._gate_seq("template_evaluations")
            record = {
                "id": f"{name}:v{version}:e{seq}",
                "seq": seq, "template": name, "version": version,
                "evaluated_by": entry["id"],
                "evaluated_at": _now(),
                "content_hash": _template_hash(template),
                "ok": health["ok"], "examples": health["examples"]}
            self.store.upsert_record("template_evaluations", record)
        self._audit(TEMPLATE_AUDIT_SCOPE, actor,
                    "template_evaluated", {
                        "template": name, "version": version,
                        "ok": health["ok"],
                        "content_hash": record["content_hash"]})
        return record

    def _standing_evaluation(self, template: TaskTemplate
                             ) -> Optional[dict]:
        """A version's standing evaluation: the latest evaluation
        record written against the version's content AS IT NOW
        HASHES — a record for other bytes is no standing at all —
        or, for a seeded version with none, its health computed
        now, marked `seeded`. None when neither exists: a
        published version nobody has evaluated."""
        digest = _template_hash(template)
        for record in reversed(self.template_evaluations(
                template.name, template.version)):
            if record["content_hash"] == digest:
                return {"ok": record["ok"],
                        "examples": record["examples"],
                        "content_hash": digest,
                        "evaluated_by": record["evaluated_by"],
                        "source": "evaluation"}
        if not template.published_by:
            health = self.template_health(template)
            return {"ok": health["ok"],
                    "examples": health["examples"],
                    "content_hash": digest, "evaluated_by": None,
                    "source": "seeded"}
        return None

    @staticmethod
    def _example_label(example: dict) -> str:
        slots = ", ".join(f"{k}={v}" for k, v in sorted(
            (example.get("slots") or {}).items()))
        return f"the example ({slots})"

    def _regression_breaks(self, candidate: TaskTemplate,
                           incumbent: TaskTemplate,
                           incumbent_standing: dict) -> list[dict]:
        """The incumbent examples the candidate breaks, judged on
        the incumbent's ruler: every example the incumbent's
        standing passes is rendered through the CANDIDATE and
        must still derive to the shape the INCUMBENT pins. An
        example the candidate's slots no longer fill, or whose
        rendered text the planner refuses or answers with a
        different shape, is broken — a promotion may change a
        template, but it may not silently change what a standing
        scenario derives."""
        from .dataset import seed_records

        scratch = InMemoryStore()
        scratch.seed_if_empty(seed_records())
        candidate_slots = {s.name for s in candidate.slots}
        breaks = []
        for example, verdict in zip(incumbent.examples,
                                    incumbent_standing["examples"]):
            if not verdict["ok"]:
                continue
            label = self._example_label(example.model_dump(
                mode="json"))
            if set(example.slots) != candidate_slots:
                breaks.append({
                    "slots": dict(example.slots),
                    "detail": f"{label} is no longer fillable: "
                              f"the candidate's slots are "
                              f"{sorted(candidate_slots)}"})
                continue
            try:
                plan = derive_plan(
                    render_pattern(candidate.pattern, example.slots),
                    scratch, self.settings.policy, "TASK-GATE")
                detail = None
                if plan_shape(plan) != expected_shape(example):
                    detail = (f"{label} derives through the "
                              f"candidate as "
                              f"{plan_shape(plan)}, not the "
                              f"shape the incumbent pins, "
                              f"{expected_shape(example)}")
            except PlanAmbiguous as amb:
                detail = (f"{label} asks a clarification through "
                          f"the candidate ({amb.code})")
            except PlanRefusal as refusal:
                detail = (f"{label} is refused through the "
                          f"candidate: {refusal.code}: "
                          f"{refusal.message}")
            if detail is not None:
                breaks.append({"slots": dict(example.slots),
                               "detail": detail})
        return breaks

    def promote_template(self, actor: str, name: str,
                         version: int) -> dict:
        """Promote one version to the name's default: a recorded
        supervisor act, and the gate itself. The refusals, in the
        order they are checked: the version must exist; it must
        not already be the default (`already_default`); the
        promoter must not be its publisher
        (`promoter_was_publisher` — the publisher's work is
        vouched for and promoted by other hands); a standing
        evaluation must exist (`no_evaluation` — or
        `hash_mismatch` when evaluations are on record but grade
        other bytes than the version's content now hashes to)
        and must pass (`evaluation_failed`, the failing examples
        named); the
        standing evaluation must not be the publisher's own
        (`evaluator_was_publisher`); and the candidate must not
        regress against the current default's standing
        (`regression`, the broken examples and the candidate's
        gains both named). Rollback is this same act aimed at a
        previous version: its standing must still be on record
        for its hash, exactly as for any candidate."""
        self._template_actor(actor, "template promotion", name)
        template = self.get_template(name, version)
        if template is None:
            self._template_refused(
                actor, "template promotion", name,
                "template_not_found",
                f"there is no template '{name}' v{version} in "
                f"the library to promote")
        current_default = self.default_template(name)
        if current_default is not None and \
                current_default.version == version:
            self._template_refused(
                actor, "template promotion", name,
                "already_default",
                f"'{name}' v{version} is already the default; "
                f"promoting it again changes nothing")
        if template.published_by and operator_named(
                template.published_by) == operator_named(actor):
            self._template_refused(
                actor, "template promotion", name,
                "promoter_was_publisher",
                f"'{actor}' published '{name}' v{version}; its "
                f"promotion is another supervisor's act, not "
                f"the publisher's")
        standing = self._standing_evaluation(template)
        if standing is None:
            recorded = self.template_evaluations(name, version)
            if recorded:
                # Evaluations exist, but none grades the content
                # as it now hashes: the bytes moved after they
                # were recorded (a store-level edit — versions are
                # never edited through the service), and a verdict
                # for other bytes is no standing.
                self._template_refused(
                    actor, "template promotion", name,
                    "hash_mismatch",
                    f"'{name}' v{version} now hashes to "
                    f"{_template_hash(template)[:12]}…, but its "
                    f"recorded evaluation graded "
                    f"{recorded[-1]['content_hash'][:12]}…; the "
                    f"content changed after evaluation — "
                    f"evaluate the version as it stands")
            self._template_refused(
                actor, "template promotion", name,
                "no_evaluation",
                f"'{name}' v{version} has no evaluation on "
                f"record for its content; evaluate it first — a "
                f"promotion is earned by a recorded pass, not "
                f"by the publishing act")
        if not standing["ok"]:
            failing = [self._example_label(e) for e in
                       standing["examples"] if not e["ok"]]
            self._template_refused(
                actor, "template promotion", name,
                "evaluation_failed",
                f"the standing evaluation of '{name}' "
                f"v{version} fails: " + "; ".join(failing))
        if standing["source"] == "evaluation" and \
                operator_named(standing["evaluated_by"] or "") == \
                operator_named(template.published_by):
            self._template_refused(
                actor, "template promotion", name,
                "evaluator_was_publisher",
                f"the standing evaluation of '{name}' v{version} "
                f"was run by its publisher, "
                f"'{template.published_by}'; a promotion needs a "
                f"pass recorded by another hand")
        if current_default is not None:
            incumbent_standing = self._standing_evaluation(
                current_default)
            if incumbent_standing is not None:
                breaks = self._regression_breaks(
                    template, current_default, incumbent_standing)
                if breaks:
                    incumbent_keys = {
                        json.dumps(e["slots"], sort_keys=True)
                        for e in incumbent_standing["examples"]}
                    gains = [e["slots"] for e in
                             standing["examples"] if e["ok"] and
                             json.dumps(e["slots"], sort_keys=True)
                             not in incumbent_keys]
                    named = "; ".join(b["detail"] for b in breaks)
                    raise_detail = {
                        "act": "template promotion", "target": name,
                        "code": "regression",
                        "reason": (f"promoting '{name}' v{version} "
                                   f"would regress the default "
                                   f"v{current_default.version}: "
                                   + named),
                        "broken": breaks, "gains": gains}
                    self._audit(TEMPLATE_AUDIT_SCOPE, actor,
                                "template_refused", raise_detail)
                    raise GateError(
                        f"template promotion refused: "
                        f"{raise_detail['reason']}")
        with self._lock:
            seq = self._gate_seq("template_promotions")
            record = {
                "id": f"{name}:p{seq}", "seq": seq,
                "template": name, "version": version,
                "promoted_by": operator_named(actor),
                "promoted_at": _now(),
                "content_hash": standing["content_hash"],
                "previous_default": (current_default.version
                                     if current_default else None),
                "evaluation_source": standing["source"],
                "evaluated_by": standing["evaluated_by"]}
            self.store.upsert_record("template_promotions", record)
        self._audit(TEMPLATE_AUDIT_SCOPE, actor,
                    "template_promoted", {
                        "template": name, "version": version,
                        "previous_default": record[
                            "previous_default"],
                        "content_hash": record["content_hash"],
                        "evaluation_source": standing["source"]})
        return record

    def template_view(self, template: TaskTemplate) -> dict:
        """One version as the library serves it: the record, with
        its health folded in, `current` naming the newest
        published version, and the round-19 gate facts — `default`
        (a name-only instantiation resolves here), `promoted`
        (the default by a promotion record, not grandfathering),
        `standing` (`seeded`, `promoted`, or `published`), the
        content hash, and the latest evaluation on record, with
        whether it graded the content as it now stands."""
        view = template.model_dump(mode="json")
        view["current"] = not template.superseded
        view["health"] = self.template_health(template)
        digest = _template_hash(template)
        view["content_hash"] = digest
        promotions = self.template_promotions(template.name)
        promoted_version = (promotions[-1]["version"]
                            if promotions else None)
        default = self.default_template(template.name)
        view["default"] = (default is not None
                           and default.version == template.version)
        view["promoted"] = promoted_version == template.version
        view["standing"] = (
            "promoted" if view["promoted"]
            else "seeded" if not template.published_by
            else "published")
        evaluations = self.template_evaluations(
            template.name, template.version)
        latest = evaluations[-1] if evaluations else None
        view["evaluation"] = None if latest is None else {
            "ok": latest["ok"],
            "evaluated_by": latest["evaluated_by"],
            "evaluated_at": latest["evaluated_at"],
            "content_hash": latest["content_hash"],
            "hash_short": latest["content_hash"][:12],
            "hash_matches": latest["content_hash"] == digest}
        return view

    def list_template_views(self) -> list[dict]:
        return [self.template_view(t) for t in self.list_templates()]

    # -- intake + planning ---------------------------------------------
    def intake(self, text: str, operator: str) -> Task:
        task_id = f"TASK-{uuid.uuid4().hex[:8].upper()}"
        task = Task(task_id=task_id, text=text, operator=operator,
                    planned_by=operator,
                    created_at=_now(), updated_at=_now())
        with self._lock:
            task.seq = self._next_seq()
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
        except PlanRefusal as refusal:
            return self._refuse_plan(task, refusal)
        return self._derive(task, {})

    def _refuse_plan(self, task: Task, refusal: PlanRefusal) -> Task:
        """Land a plan-time refusal on a task: REFUSED, the reason
        on the record, the act on the trail. Shared by intake and
        by derivation after a clarification answer — a refusal is
        a refusal, whichever door derivation took to reach it."""
        task.status = TaskStatus.REFUSED
        task.refusal_reason = f"{refusal.code}: {refusal.message}"
        task.updated_at = _now()
        self.store.put_task(task.model_dump(mode="json"))
        self._audit(task.task_id, "policy", "plan_refused", {
            "code": refusal.code, "reason": refusal.message})
        return task

    def _derive(self, task: Task,
                resolutions: dict[str, str]) -> Task:
        """Derive the task's plan from the records as they stand,
        with any recorded clarification answers pinned under their
        keys. Three landings: a plan (PLANNED, stored, the
        ordinary path — including the check that the plan fits
        the planner's role allowlist, every page it works on and
        every field it touches), a refusal (REFUSED, policy's
        word), or a question (AWAITING_CLARIFICATION, landed by
        `_ask_or_exhaust`)."""
        try:
            plan = derive_plan(task.text, self.store,
                               self.settings.policy, task.task_id,
                               resolutions)
            role = self.role_of(task.planned_by or task.operator)
            if role is not None:
                problems = plan_problems(role, plan)
                if problems:
                    first = problems[0]
                    raise PlanRefusal(
                        first["problem"],
                        f"refused: {describe_problem(role, first)}; "
                        f"the plan exceeds the planner's role")
        except PlanAmbiguous as amb:
            return self._ask_or_exhaust(task, amb)
        except PlanRefusal as refusal:
            return self._refuse_plan(task, refusal)
        plan.created_at = _now()
        plan.preconditions = capture_preconditions(self.store, plan)
        self.store.put_plan(plan.model_dump(mode="json"))
        task.status = TaskStatus.PLANNED
        task.plan_version = plan.version
        task.updated_at = _now()
        self.store.put_task(task.model_dump(mode="json"))
        self._audit(task.task_id, "planner", "plan_created", {
            "plan_version": plan.version, "task_type": plan.task_type,
            "summary": plan.summary, "steps": len(plan.steps)})
        return task

    def _ask_or_exhaust(self, task: Task, amb: PlanAmbiguous) -> Task:
        """Land a derivation ambiguity on the task: park it in
        AWAITING_CLARIFICATION carrying the question and its
        options — unless the task has already spent its governed
        patience (`Policy.max_clarifications` distinct questions),
        in which case a further ambiguity refuses the task with
        the history attached. Being asked again about a question
        already asked — the records moved under a pinned answer,
        so the choice must be re-made — spends no new patience."""
        asked_keys = {c.key for c in task.clarifications}
        if (amb.key not in asked_keys
                and len(asked_keys)
                >= self.settings.policy.max_clarifications):
            history = [{
                "key": c.key, "code": c.code,
                "question": c.question, "answer": c.answer,
                "answerer": c.answerer}
                for c in task.clarifications]
            trail = "; ".join(
                f"{h['question']} -> {h['answer']}" for h in history)
            reason = (
                f"the task is still ambiguous after "
                f"{len(asked_keys)} clarifications ({trail}) and "
                f"one more choice remains ({amb.question}); the "
                f"shell asks at most "
                f"{self.settings.policy.max_clarifications} "
                f"questions of one task "
                f"(COPILOT_MAX_CLARIFICATIONS) and invents "
                f"nothing — state the task with the remaining "
                f"choices made")
            task.status = TaskStatus.REFUSED
            task.refusal_reason = (
                f"clarification_exhausted: {reason}")
            task.updated_at = _now()
            self.store.put_task(task.model_dump(mode="json"))
            self._audit(task.task_id, "policy", "plan_refused", {
                "code": "clarification_exhausted", "reason": reason,
                "clarifications": history})
            return task
        task.clarifications.append(Clarification(
            key=amb.key, code=amb.code, question=amb.question,
            options=[ClarificationOption(**o) for o in amb.options]))
        task.status = TaskStatus.AWAITING_CLARIFICATION
        task.updated_at = _now()
        self.store.put_task(task.model_dump(mode="json"))
        self._audit(task.task_id, "planner", "clarification_asked", {
            "key": amb.key, "code": amb.code,
            "question": amb.question,
            "options": [dict(o) for o in amb.options]})
        return task

    def answer_clarification(self, task_id: str, operator: str,
                             choice: str) -> Task:
        """Answer the task's pending clarification by picking one
        of its options — a recorded act under the planning role
        rules: the answerer must be enrolled, active, and hold a
        plan-capable role, the same checks intake runs, recorded
        the way the confirmation gate records its refusals. The
        planner MAY answer their own clarification: it is still
        their task and no gate is being passed; confirmation
        remains a second person, as always.

        The choice must be one of the offered options' values.
        Anything else — free text, a plausible-sounding phrase —
        is refused with the options restated, and the task stays
        where it was: the shell never interprets a reply into a
        value. A taken answer is stored on the question, the act
        is audited, and derivation re-runs with the choice
        pinned: the task proceeds to PLANNED, asks one further
        question, or refuses — the same three landings as
        intake."""
        task = self._require_task(task_id)
        if task.status != TaskStatus.AWAITING_CLARIFICATION:
            raise GateError(
                f"task {task_id} is {task.status.value}; only a "
                f"task awaiting clarification can be answered")
        pending = task.pending_clarification
        if pending is None:  # the state implies one; fail closed
            raise GateError(
                f"task {task_id} awaits clarification but carries "
                f"no pending question")

        def refused(code: str, reason: str) -> None:
            self._audit(task_id, operator, "clarification_refused", {
                "code": code, "reason": reason,
                "key": pending.key, "question": pending.question})
            raise GateError(
                f"clarification of task {task_id} refused: {reason}")

        entry = self.directory_entry(operator)
        role = self.role_of(operator)
        if entry is None or role is None:
            refused(
                "unknown_operator",
                f"'{operator}' holds no role in the operator "
                f"directory; an unknown operator may not answer a "
                f"clarification")
        if not self.entry_active(entry):
            refused(
                "operator_inactive",
                f"'{operator}' is deactivated (operator_inactive); "
                f"a deactivated operator may not answer a "
                f"clarification")
        if not role.can_plan:
            refused(
                "role_cannot_plan",
                f"role '{role.name}' may not plan work; answering "
                f"a clarification is part of planning the task, "
                f"and planning is not among its action classes")
        choice = (choice or "").strip()
        if choice not in [o.value for o in pending.options]:
            offered = "; ".join(
                f"{o.value} ({o.label})" for o in pending.options)
            refused(
                "not_an_option",
                f"'{choice}' is not one of the options. The "
                f"question stands: {pending.question} The options "
                f"are: {offered}. Pick one by its value — the "
                f"shell does not interpret free text into a value")
        pending.answer = choice
        pending.answerer = operator
        task.updated_at = _now()
        self.store.put_task(task.model_dump(mode="json"))
        self._audit(task_id, operator, "clarification_answered", {
            "key": pending.key, "code": pending.code,
            "question": pending.question, "answer": choice})
        return self._derive(task, task.clarification_resolutions())

    def intake_transcript(self, text: str, operator: str) -> Task:
        """A task stated by voice, as transcribed (round 13):
        the transcript's envelope — speaker labels, timestamp
        markers, line breaks — is normalised in code, and the
        cleaned text goes through the ordinary `intake`, so the
        plan a transcript produces is the plan the same words
        typed produce, under the same identity, role, and
        policy checks. The task's text is the normalised
        statement; the raw transcript rides the task's trail
        (`transcript_intake`, with both forms) so what was
        actually dictated stays attributable. Numbers are not
        converted: ids and amounts must arrive as digits (see
        spoken.py) — a transcript whose figures are words is
        refused by the grammar like any underivable text."""
        normalized = normalize_transcript(text)
        task = self.intake(normalized, operator)
        self._audit(task.task_id, operator, "transcript_intake", {
            "raw": str(text), "normalized": normalized})
        return task

    # -- the MCP door -----------------------------------------------------
    # Round 17: machine clients (an MCP host — an IDE, a desktop
    # assistant, another agent) submit work through these doors and
    # no others. Each is the ordinary act plus one entry on the
    # task's trail naming the channel, the way `intake_transcript`
    # rides the intake it wraps: the planner, the role rules, and
    # policy cannot tell an MCP submission from a typed one, and
    # the trail can — every act the surface causes carries
    # `channel: "mcp"` on its door entry. The surface itself
    # (mcp_server.py) holds no logic of its own beyond the
    # protocol: it cannot confirm, execute, amend, or touch a
    # record, because no door here leads to those acts.

    def submit_task_mcp(self, text: str, operator: str) -> Task:
        """A task submitted by a machine client: ordinary intake,
        with the door on the trail. A refusal lands exactly as it
        does at the console — a REFUSED task carrying policy's or
        the directory's own words — because it is the same intake
        producing it."""
        task = self.intake(text, operator)
        self._audit(task.task_id, operator, "mcp_task_submitted", {
            "channel": "mcp", "tool": "submit_task"})
        return task

    def instantiate_template_mcp(self, name: str,
                                 version: Optional[int],
                                 slots: dict[str, str],
                                 operator: str) -> Task:
        """A template task submitted by a machine client: ordinary
        instantiation (slot validation, the rendered text through
        the ordinary planner), with the door on the trail. Slot
        refusals raise `TemplateError` before any task exists, as
        they do at the API; the refusal is recorded in the library
        scope by instantiation itself."""
        task = self.instantiate_template(name, version, slots,
                                         operator)
        self._audit(task.task_id, operator, "mcp_template_submitted", {
            "channel": "mcp", "tool": "submit_template_task",
            "template": name, "version": task.template_version})
        return task

    def answer_clarification_mcp(self, task_id: str, operator: str,
                                 choice: str) -> Task:
        """A clarification answered by a machine client: the
        ordinary recorded act — the answerer checked against the
        directory and the planning rules, the choice required to
        be one of the offered options — with the door on the
        trail. A refused answer raises before the door entry is
        written; the refusal itself is on the trail already,
        recorded by `answer_clarification`."""
        task = self.answer_clarification(task_id, operator, choice)
        self._audit(task_id, operator, "mcp_clarification_answered", {
            "channel": "mcp", "tool": "answer_clarification",
            "answer": choice})
        return task

    # -- reads ----------------------------------------------------------
    def get_task(self, task_id: str) -> Optional[Task]:
        raw = self.store.get_task(task_id)
        return Task(**raw) if raw else None

    def list_tasks(self) -> list[Task]:
        """Every task, in the order the work arrived — the
        creation sequence the service stamped at first storage.
        The store cannot carry this order (the SQL profile reads
        tasks back by their random ids, the in-memory profile by
        insertion), so the sort lives here, where the sequence
        is assigned. Tasks stored before the sequence existed
        carry 0 and fall back to (created_at, task_id) behind
        it. Every reader that walks the task list — the API's
        task list, the orphaned-work read, the correction
        chains — inherits this order."""
        tasks = [Task(**t) for t in self.store.list_tasks()]
        return sorted(tasks,
                      key=lambda t: (t.seq, t.created_at, t.task_id))

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
        # Dual control (round 18): the seats standing on the old
        # version bound THAT version's words. The amendment's
        # version starts with none — a plan that had one of its
        # two confirmations owes both again. The seats are not
        # lost to history: each is a `plan_confirmed` entry in
        # the trail, and the reset count rides this entry.
        seats_reset = len(task.confirmations_for(current.version))
        task.confirmations = []
        self._store_plan_version(task, amended)
        self._save(task)
        self._audit(task_id, operator, "plan_amended", {
            "from_version": current.version,
            "to_version": amended.version,
            "confirmations_reset": seats_reset,
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
        # The answers the task already carries ride along: a
        # clarified task that went stale is re-derived with its
        # choices pinned, not asked about again. If the records
        # as they now stand raise a question those answers no
        # longer settle, the re-plan refuses — a re-plan is not
        # an interactive door and cannot ask; the route onward
        # is a fresh task, where the choice can be made.
        refusal: Optional[PlanRefusal] = None
        plan: Optional[Plan] = None
        try:
            plan = derive_plan(task.text, self.store,
                               self.settings.policy, task_id,
                               task.clarification_resolutions())
        except PlanAmbiguous as amb:
            refusal = PlanRefusal(
                "clarification_needed",
                f"refused: re-deriving from the records as they "
                f"now stand raises a question a re-plan cannot "
                f"ask ({amb.question}); the answers on the task "
                f"no longer settle it — state the task afresh so "
                f"the choice can be made")
        except PlanRefusal as exc:
            refusal = exc
        if refusal is not None:
            task.status = TaskStatus.REFUSED
            task.refusal_reason = f"{refusal.code}: {refusal.message}"
            task.stale_records = []
            self._save(task)
            self._audit(task_id, "policy", "plan_refused", {
                "code": refusal.code, "reason": refusal.message,
                "during": "replan"})
            return task
        assert plan is not None
        plan.version = previous.version + 1
        plan.created_at = _now()
        plan.preconditions = capture_preconditions(self.store, plan)
        self._store_plan_version(task, plan)
        task.status = TaskStatus.PLANNED
        task.confirmed_by = None
        task.confirmed_version = None
        task.confirmations = []
        task.stale_records = []
        self._save(task)
        self._audit(task_id, operator, "plan_replanned", {
            "from_version": previous.version,
            "to_version": plan.version,
            "superseded_version": previous.version})
        return task

    # -- the gate ---------------------------------------------------------
    def _check_confirmer(self, task: Task, operator: str) -> None:
        """The confirmation gate's identity checks, shared by
        both confirmation paths (the console's confirm and the
        read-back confirm, round 13): the task must be PLANNED;
        the confirmer must be enrolled and active, hold a
        confirm-capable role, and not be the planner —
        self-confirmation is refused for every role (names
        compare normalised, so a spacing or casing variant of
        the planner's name is still the planner). Every refusal
        is recorded before it raises — including the deactivated
        confirmer's: a name that may perform no act is refused
        as itself, not as a stranger."""
        if task.status != TaskStatus.PLANNED:
            raise GateError(
                f"task {task.task_id} is {task.status.value}; only "
                f"a PLANNED task can be confirmed")
        entry = self.directory_entry(operator)
        if entry is not None and not self.entry_active(entry):
            self._audit(task.task_id, operator,
                        "confirmation_refused", {
                            "code": "operator_inactive",
                            "reason": f"'{operator}' is deactivated; "
                                      f"a deactivated operator may "
                                      f"not confirm plans",
                            "planner": task.operator,
                            "plan_version": task.plan_version})
            raise GateError(
                f"confirmation of task {task.task_id} refused: "
                f"'{operator}' is deactivated (operator_inactive)")
        role = self.role_of(operator)
        if role is None or not role.can_confirm:
            code = ("unknown_operator" if role is None
                    else "role_cannot_confirm")
            reason = (f"'{operator}' holds no role" if role is None
                      else f"role '{role.name}' may not confirm "
                            f"plans")
            self._audit(task.task_id, operator,
                        "confirmation_refused", {
                            "code": code, "reason": reason,
                            "planner": task.operator,
                            "plan_version": task.plan_version})
            raise GateError(
                f"confirmation of task {task.task_id} refused: "
                f"{reason}")
        if operator_named(operator) == operator_named(task.operator):
            self._audit(task.task_id, operator,
                        "confirmation_refused", {
                            "code": "self_confirmation",
                            "reason": "the planner may not confirm "
                                      "their own plan",
                            "planner": task.operator,
                            "plan_version": task.plan_version})
            raise GateError(
                f"confirmation of task {task.task_id} refused: "
                f"'{operator}' planned this task; a plan is "
                f"confirmed by a second person, never its planner")
        # Dual control (round 18): a seat already taken on this
        # plan version cannot be taken again by the same person —
        # the second seat exists to be a SECOND pair of eyes, so
        # a repeat confirmation from the first confirmer (under
        # any spacing or casing of the name) fills nothing.
        for seat in task.confirmations_for(task.plan_version):
            if operator_named(seat.operator) == operator_named(
                    operator):
                self._audit(task.task_id, operator,
                            "confirmation_refused", {
                                "code": "already_confirmed",
                                "reason": f"'{seat.operator}' has "
                                          f"already confirmed "
                                          f"plan "
                                          f"v{task.plan_version}; "
                                          f"dual control needs a "
                                          f"second, different "
                                          f"confirmer",
                                "planner": task.operator,
                                "plan_version": task.plan_version})
                raise GateError(
                    f"confirmation of task {task.task_id} refused: "
                    f"'{operator}' has already confirmed plan "
                    f"v{task.plan_version} (already_confirmed); "
                    f"dual control needs a second, different "
                    f"confirmer")

    def _finalize_confirmation(self, task: Task, operator: str,
                               extra: Optional[dict] = None) -> Task:
        """Land a confirmation that has passed every check, as a
        SEAT on the current plan version (round 18). The seat is
        recorded on the task and in the trail (`plan_confirmed`,
        naming the seat, the seats the gate requires, and
        whether the gate completed); the gate completes — the
        state moves to CONFIRMED and the confirmation binds this
        exact plan version — when the seats standing on the
        version reach the number the plan's stakes require: one
        for an ordinary plan, two distinct confirmers for a plan
        under dual control (see `policy.dual_control_assessment`).
        Until then the task stands PLANNED with its seat filled,
        and execution refuses, naming the second confirmer it is
        owed. Execution loads the bound version by number, so
        nothing that happens to the plan store afterwards can
        widen what the humans approved. `extra` carries the
        read-back path's binding (its method, the spoken hash,
        the token) into the same `plan_confirmed` entry, so the
        trail shows how the plan was confirmed, not only that
        it was."""
        plan = self.get_plan(task.task_id)
        assessment = (dual_control_assessment(
            plan, self.settings.policy) if plan is not None else {
                "required": False, "reasons": []})
        seats_required = 2 if assessment["required"] else 1
        method = (extra or {}).get("method", "confirm")
        task.confirmations = [
            seat for seat in task.confirmations
            if seat.plan_version == task.plan_version]
        task.confirmations.append(PlanConfirmation(
            operator=operator, method=method,
            plan_version=task.plan_version or 0, at=_now()))
        seats = task.confirmations_for(task.plan_version)
        detail: dict = {
            "plan_version": task.plan_version,
            "seat": len(seats),
            "seats_required": seats_required,
            "dual_control": assessment["required"],
            "complete": len(seats) >= seats_required}
        if extra:
            detail.update(extra)
        if len(seats) >= seats_required:
            task.status = TaskStatus.CONFIRMED
            task.confirmed_by = operator
            task.confirmed_version = task.plan_version
            detail["confirmed_version"] = task.confirmed_version
        else:
            detail["awaiting"] = "second_confirmer"
            detail["dual_control_reasons"] = assessment["reasons"]
        self._save(task)
        self._audit(task.task_id, operator, "plan_confirmed", detail)
        return task

    def confirm(self, task_id: str, operator: str) -> Task:
        task = self._require_task(task_id)
        self._check_confirmer(task, operator)
        return self._finalize_confirmation(task, operator)

    def dual_control_view(self, task: Task) -> dict:
        """The dual-control fact for one task, as its reads show
        it: whether the plan the task stands on needs two
        confirmers and why (`policy.dual_control_assessment`,
        computed from the plan — the bound plan once confirmed,
        else the current one), the seats the gate requires and
        the seats standing on that plan's version, who holds
        them, and whether a second confirmer is still owed.
        Derived on every read from the plan and the task's seat
        records — never stored — so the view cannot disagree
        with the gate that will judge the next confirmation."""
        plan: Optional[Plan] = None
        if task.plan_version is not None:
            version = (task.confirmed_version
                       if task.status != TaskStatus.PLANNED
                       and task.confirmed_version is not None
                       else task.plan_version)
            plan = self.get_plan(task.task_id, version)
        if plan is None:
            return {"required": False, "reasons": [],
                    "total_credited": "0.00",
                    "high_stakes_fields": [],
                    "threshold": str(
                        self.settings.policy.dual_control_cap),
                    "seats_required": 1, "seats_filled": 0,
                    "confirmers": [],
                    "awaiting_second_confirmer": False}
        assessment = dual_control_assessment(
            plan, self.settings.policy)
        seats = task.confirmations_for(plan.version)
        required = assessment["required"]
        return {
            **assessment,
            "seats_required": 2 if required else 1,
            "seats_filled": len(seats),
            "confirmers": [seat.operator for seat in seats],
            "awaiting_second_confirmer": bool(
                required and task.status == TaskStatus.PLANNED
                and len(seats) == 1),
        }

    # -- accessibility: the spoken form and read-back confirmation -------
    # Round 13. The spoken form is a view of the stored plan
    # (spoken.py), rendered in code at the moment of asking; the
    # read-back path is the same gate with one binding added:
    # the confirmer presents the token the shell issued when the
    # plan was read to them, bound to the task, the plan version,
    # the spoken form's hash, and the person it was issued to.
    # Amend the plan after the read-back and the version moves,
    # the words change, and the token can no longer confirm —
    # the confirmer must hear the version they approve.

    def spoken_view(self, task_id: str,
                    version: Optional[int] = None) -> dict:
        """The current plan (or a named version) in its spoken
        form, with the hash read-backs bind to. A read: asking
        stores nothing and changes nothing."""
        task = self._require_task(task_id)
        plan = self.get_plan(task_id, version)
        if plan is None:
            raise GateError(
                f"task {task_id} has no plan to read aloud"
                + (f" at version {version}" if version else ""))
        return {
            "task_id": task_id,
            "plan_version": plan.version,
            "pages": plan.pages,
            "multi_page": plan.is_multi_page,
            "spoken": spoken_plan(plan),
            "spoken_hash": spoken_hash(plan),
        }

    def issue_readback(self, task_id: str, operator: str) -> dict:
        """Issue a read-back token for a PLANNED task's current
        plan version: the spoken form is rendered, and the token
        record binds (task, plan version, spoken hash, the
        operator it is issued to). The asker must be an
        enrolled, active operator — a name that may perform no
        act cannot be handed a confirmation token; the refusal
        is recorded (`readback_refused`) before it raises, like
        the other identity refusals."""
        task = self._require_task(task_id)
        if task.status != TaskStatus.PLANNED:
            raise GateError(
                f"task {task_id} is {task.status.value}; only a "
                f"PLANNED task's plan can be read back for "
                f"confirmation")
        entry = self.directory_entry(operator)
        if entry is None or not self.entry_active(entry):
            code = ("unknown_operator" if entry is None
                    else "operator_inactive")
            reason = (f"'{operator}' holds no role in the "
                      f"operator directory"
                      if entry is None else
                      f"'{operator}' is deactivated "
                      f"(operator_inactive); a deactivated "
                      f"operator may not confirm plans")
            self._audit(task_id, operator, "readback_refused", {
                "code": code, "reason": reason,
                "plan_version": task.plan_version})
            raise GateError(
                f"read-back for task {task_id} refused: {reason}")
        view = self.spoken_view(task_id)
        token = uuid.uuid4().hex
        self.store.upsert_record("readbacks", {
            "id": token, "task_id": task_id,
            "plan_version": view["plan_version"],
            "spoken_hash": view["spoken_hash"],
            "issued_to": entry["id"], "issued_at": _now(),
            "used_at": ""})
        self._audit(task_id, operator, "readback_issued", {
            "plan_version": view["plan_version"],
            "spoken_hash": view["spoken_hash"],
            "issued_to": entry["id"]})
        return {"token": token, **view, "issued_to": entry["id"]}

    def _readback_refused(self, task: Task, operator: str,
                          code: str, reason: str) -> None:
        self._audit(task.task_id, operator,
                    "confirmation_refused", {
                        "code": code, "reason": reason,
                        "method": "readback",
                        "planner": task.operator,
                        "plan_version": task.plan_version})
        raise GateError(
            f"confirmation of task {task.task_id} refused: {reason}")

    def confirm_readback(self, task_id: str, operator: str,
                         token: str) -> Task:
        """Confirm by read-back: every ordinary confirmation
        rule runs first, unchanged — then the token must be
        the one this shell issued for this task, to this
        operator, unspent, and bound to the plan version that
        still stands. A plan amended after the read-back has a
        new version and new spoken words, so the old token
        refuses as `readback_stale`, naming both versions: the
        confirmer must hear the plan they approve."""
        task = self._require_task(task_id)
        self._check_confirmer(task, operator)
        record = self.store.get_record("readbacks", token or "")
        if record is None or record.get("task_id") != task_id:
            self._readback_refused(
                task, operator, "readback_unknown",
                "the read-back token was not issued for this "
                "task; ask for a fresh read-back of the plan")
        if operator_named(record.get("issued_to", "")) \
                != operator_named(operator):
            self._readback_refused(
                task, operator, "readback_operator_mismatch",
                f"the read-back was issued to "
                f"'{record.get('issued_to')}'; the operator who "
                f"heard the plan is the operator who confirms it")
        if record.get("used_at"):
            self._readback_refused(
                task, operator, "readback_used",
                "the read-back token has already been spent; "
                "ask for a fresh read-back of the plan")
        plan = self.get_plan(task_id)
        assert plan is not None
        if record.get("plan_version") != task.plan_version \
                or record.get("spoken_hash") != spoken_hash(plan):
            self._readback_refused(
                task, operator, "readback_stale",
                f"the read-back was of plan "
                f"v{record.get('plan_version')}, but the plan "
                f"now stands at v{task.plan_version}; the words "
                f"have changed since they were heard — hear the "
                f"new version and confirm that")
        record = {**record, "used_at": _now()}
        self.store.upsert_record("readbacks", record)
        return self._finalize_confirmation(task, operator, {
            "method": "readback",
            "spoken_hash": record["spoken_hash"],
            "readback_token": record["id"]})

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
                started_at=_now(),
                # The budget, snapshotted from Policy at claim
                # time: the attempt runs under the ceilings in
                # force when it started, whatever Policy says
                # later — the record states its own budget.
                step_ceiling=self.settings.policy
                .max_execution_steps,
                seconds_ceiling=self.settings.policy
                .max_execution_seconds,
                # The deployment, snapshotted the same way: the
                # profile this attempt runs under — its name,
                # how the bundle was delivered, where the model
                # lives, and the model name. Never the key.
                deployment_profile=self.settings.profile.name,
                bundle_delivery=self.settings.profile.delivery,
                endpoint_class=self.settings.profile
                .endpoint_class,
                model=self.settings.page_agent.model)
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

    # -- budgets -----------------------------------------------------
    # The ceilings an attempt runs under are snapshotted on the
    # attempt at claim time (see _claim_attempt); the checks below
    # read the attempt's own copies, never Policy afresh, so an
    # attempt's budget cannot move mid-run. The attempt is the
    # budget unit: its consumed steps are its own outcomes, and a
    # resume or restart — a new attempt — starts from zero.

    @staticmethod
    def _elapsed_seconds(attempt: ExecutionAttempt) -> float:
        """The attempt's wall clock so far, from its recorded
        start to now — the same `_now` stamps every record
        carries, so the measure is the record's own."""
        started = datetime.fromisoformat(attempt.started_at)
        return (datetime.now(timezone.utc) - started).total_seconds()

    def _budget_block(self, attempt: ExecutionAttempt,
                      step: PlanStep) -> Optional[str]:
        """Why the budget forbids running (or accepting) `step`
        next on this attempt — or None when it may proceed. The
        wall clock is the outer bound and is read first: an
        attempt that has already spanned its seconds has no steps
        left in it, whatever the count says. The returned text is
        the exhaustion's own wording: it names the ceiling and
        the counts, and it is recorded verbatim — on the attempt,
        in the audit, and on the task as its failure reason."""
        elapsed = self._elapsed_seconds(attempt)
        if elapsed >= attempt.seconds_ceiling:
            return (
                f"budget exhausted: this execution has run "
                f"{int(elapsed)}s of its wall-clock ceiling of "
                f"{attempt.seconds_ceiling}s; step {step.seq} "
                f"was not run")
        if len(attempt.steps) >= attempt.step_ceiling:
            return (
                f"budget exhausted: this attempt has used its "
                f"{attempt.step_ceiling} execution steps "
                f"(ceiling {attempt.step_ceiling}); step "
                f"{step.seq} was not run")
        return None

    def _exhaust_execution(self, task: Task, plan: Plan,
                           attempt: ExecutionAttempt,
                           step: PlanStep, reason: str,
                           driver: str) -> Task:
        """Stop an attempt for budget, through the ordinary
        failure machinery: the un-run step is recorded as the
        attempt's failed step with the exhaustion as its error,
        the attempt carries the exhaustion on its own fields,
        and the task lands in EXECUTION_FAILED naming it — the
        partial state stands, and the exits are the failure
        exits (resume, restart, abandon), each a new attempt
        with a fresh budget. There is no silent overrun and no
        budget-specific terminal state to hide in."""
        failed = StepOutcome(seq=step.seq, driver=driver,
                             ok=False, detail=reason)
        attempt.steps.append(failed)
        attempt.budget_exhausted = True
        attempt.exhaustion_reason = reason
        self._store_attempt(attempt)
        self._audit(task.task_id, driver, "budget_exhausted", {
            "attempt": attempt.number, "mode": attempt.mode,
            "plan_version": plan.version,
            "seq": step.seq, "reason": reason,
            "steps_consumed": len(attempt.steps) - 1,
            "step_ceiling": attempt.step_ceiling,
            "seconds_ceiling": attempt.seconds_ceiling})
        return self._fail_execution(task, plan, attempt, failed)

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
            "budget_exhausted": attempt.budget_exhausted,
            "exhaustion_reason": attempt.exhaustion_reason,
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

    def _record_navigation(self, task: Task, plan: Plan,
                           attempt: ExecutionAttempt, seq: int,
                           from_page: str, to_page: str,
                           driver: str) -> None:
        """Record one page transition on the running attempt and
        in the audit trail: before step `seq` ran, the driver
        moved from `from_page` to `to_page` (round 12). A
        navigation is not a step — it consumes no step budget and
        changes no record; it is the execution's evidence of
        where it stood between the steps."""
        attempt.navigations.append(PageNavigation(
            seq=seq, from_page=from_page, to_page=to_page,
            driver=driver))
        self._store_attempt(attempt)
        self._audit(task.task_id, driver, "page_navigated", {
            "seq": seq, "from_page": from_page, "to_page": to_page,
            "attempt": attempt.number, "plan_version": plan.version})

    def _current_page(self, task_id: str, plan: Plan,
                      base_attempt: int) -> Optional[str]:
        """The console page an execution stands on: the page of
        its latest act — an accepted step outcome or a recorded
        navigation — across every attempt the execution counts
        (from `base_attempt` on, so a resume stands where its
        earlier attempts left it and a restart stands only on its
        own). None before the first step lands: the execution has
        not stood anywhere yet, and the first step needs no
        navigation."""
        page_of = {s.seq: s.page for s in plan.steps}
        current: Optional[str] = None
        for attempt in self.get_attempts(task_id):
            if attempt.number < base_attempt:
                continue
            navs = list(attempt.navigations)
            for outcome in attempt.steps:
                # A navigation precedes the step it enables: any
                # navigation recorded for this step or an earlier
                # one landed before this outcome did.
                while navs and navs[0].seq <= outcome.seq:
                    current = navs.pop(0).to_page
                if outcome.seq in page_of:
                    current = page_of[outcome.seq]
            for nav in navs:
                current = nav.to_page
        return current

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
        # Where the execution stands as this attempt starts: a
        # resume continues mid-plan, and the step the plan ran
        # just before this attempt's first step is where it stood
        # — a first step on a different page crosses one.
        prev_page: Optional[str] = None
        if steps:
            first = next(i for i, s in enumerate(plan.steps)
                         if s.seq == steps[0].seq)
            if first > 0:
                prev_page = plan.steps[first - 1].page
        for step in steps:
            # The budget is read before the step runs: a step the
            # ceiling forbids is never handed to the driver at
            # all, so nothing it would have written can land.
            block = self._budget_block(attempt, step)
            if block is not None:
                return self._exhaust_execution(
                    task, plan, attempt, step, block, DRIVER_OFFLINE)
            # A page change between consecutive steps is an
            # explicit navigation on the attempt — for this
            # driver a recorded no-op transition naming the page
            # left and the page arrived on, before the step runs.
            if prev_page is not None and step.page != prev_page:
                self._record_navigation(
                    task, plan, attempt, step.seq, prev_page,
                    step.page, DRIVER_OFFLINE)
            prev_page = step.page
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
        if task.status == TaskStatus.PLANNED:
            # Dual control (round 18): a plan whose stakes put it
            # under dual control stands PLANNED with its first
            # seat filled until a second, different confirmer
            # lands — say exactly that, rather than the bare
            # "unconfirmed" the generic refusal below would give.
            seats = task.confirmations_for(task.plan_version)
            current = self.get_plan(task_id)
            if seats and current is not None \
                    and dual_control_assessment(
                        current, self.settings.policy)["required"]:
                raise GateError(
                    f"execution of task {task_id} refused: plan "
                    f"v{task.plan_version} is under dual control "
                    f"and stands at one of two confirmations "
                    f"({seats[0].operator}); a second, different "
                    f"confirmer must confirm it before it can "
                    f"execute")
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
                                "page": first["page"],
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
            self._audit(task_id, executor, "execution_started", {
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
        step = next(s for s in plan.steps if s.seq == seq)
        # Navigation (round 12): the report claims work on the
        # step's page. If this execution last stood on a different
        # page, a navigation to this one must already be recorded
        # — a recorded navigation is what _current_page reads, so
        # standing elsewhere here means none was reported. The
        # report is refused as a bridge protocol violation and the
        # refusal recorded; the attempt stays running, so the
        # bridge can navigate and report again. A step report may
        # never silently cross pages.
        current = self._current_page(task_id, plan,
                                     attempt.base_attempt)
        if current is not None and current != step.page:
            self._audit(task_id, DRIVER_LIVE, "step_report_refused", {
                "code": "navigation_missing", "seq": seq,
                "step_page": step.page, "current_page": current,
                "attempt": attempt.number,
                "plan_version": plan.version,
                "reason": f"step {seq} works on the {step.page} "
                          f"page, but the execution stands on the "
                          f"{current} page and no navigation was "
                          f"reported"})
            raise GateError(
                f"step report for task {task_id} refused: step "
                f"{seq} works on the {step.page} page, but this "
                f"execution stands on the {current} page and no "
                f"navigation {current} -> {step.page} was reported "
                f"(navigation_missing); the bridge must report the "
                f"navigation before reporting work on a new page")
        detail = report.detail
        if not report.ok and not detail:
            detail = ("the in-page agent reported this step failed "
                      "and gave no detail")
        outcome = StepOutcome(seq=seq, driver=DRIVER_LIVE,
                              ok=report.ok, detail=detail)
        self._audit(task_id, DRIVER_LIVE, "step_reported", {
            "seq": seq, "ok": report.ok, "detail": report.detail,
            "attempt": attempt.number, "plan_version": plan.version})
        # The report stands in the trail as data either way — it
        # arrived. But a report that lands past the attempt's
        # budget is not accepted: the shell stops accepting
        # reports at the ceiling, and the execution fails for
        # budget with the steps that did land standing. (The
        # bridge may already have done the step's work in the
        # page; that state stands too, named in the attempt's
        # state lines like any partial state — nothing is
        # verified, and nothing is silently overrun.)
        block = self._budget_block(attempt, step)
        if block is not None:
            return self._exhaust_execution(
                task, plan, attempt, step, block, DRIVER_LIVE)
        attempt.steps.append(outcome)
        self._store_attempt(attempt)
        if not report.ok:
            return self._fail_execution(task, plan, attempt, outcome)
        if self._completed_seqs(task_id, attempt.base_attempt) != \
                {s.seq for s in plan.steps}:
            return task
        return self._complete_attempt(task, plan, attempt)

    def report_navigation(self, task_id: str,
                          report: NavigationReportIn) -> Task:
        """Record the live bridge's navigation between pages
        (round 12): it stood on `from_page` and now stands on
        `to_page`, ahead of the step that works there.

        The report is protocol, and the shell checks it like any
        other protocol claim: it must start where the execution
        actually stands (the page of its latest accepted act) and
        arrive on the page of the next step the execution still
        owes — step reports are then accepted on that page. A
        navigation that does not fit is refused and recorded
        (`navigation_refused`, with its code); the attempt stays
        running. The first step of an execution needs no
        navigation: the bridge arms on its page."""
        task = self._require_task(task_id)
        if task.status != TaskStatus.CONFIRMED or task.driver != DRIVER_LIVE:
            raise GateError(
                f"task {task_id} is not awaiting live navigation "
                f"reports (status {task.status.value}, driver "
                f"{task.driver})")
        plan = self._bound_plan(task)
        attempt = self._running_attempt(task_id)
        if attempt is None:
            raise GateError(
                f"task {task_id} has no execution attempt in flight "
                f"to navigate against")
        current = self._current_page(task_id, plan,
                                     attempt.base_attempt)

        def refuse(code: str, reason: str) -> None:
            self._audit(task_id, DRIVER_LIVE, "navigation_refused", {
                "code": code, "from_page": report.from_page,
                "to_page": report.to_page, "current_page": current,
                "attempt": attempt.number,
                "plan_version": plan.version, "reason": reason})
            raise GateError(
                f"navigation report for task {task_id} refused: "
                f"{reason}")

        if current is None:
            refuse("no_current_page",
                   "no step of this execution has landed yet; the "
                   "first step needs no navigation — the bridge "
                   "arms on the first step's page")
        if report.from_page != current:
            refuse("from_page_mismatch",
                   f"the bridge reports leaving the "
                   f"{report.from_page} page, but this execution "
                   f"stands on the {current} page; a navigation "
                   f"must start where the execution actually stands")
        done = self._completed_seqs(task_id, attempt.base_attempt)
        remaining = [s for s in plan.steps if s.seq not in done]
        if not remaining:
            refuse("no_step_owing",
                   "every step of this execution has landed; "
                   "there is no step to navigate to")
        nxt = remaining[0]
        if nxt.page == current:
            refuse("no_navigation_owed",
                   f"step {nxt.seq}, the next step this execution "
                   f"owes, works on the {current} page the "
                   f"execution already stands on; no navigation "
                   f"is owed")
        if report.to_page != nxt.page:
            refuse("unexpected_destination",
                   f"the next step this execution owes is step "
                   f"{nxt.seq} on the {nxt.page} page, but the "
                   f"bridge reports arriving on the "
                   f"{report.to_page} page")
        self._record_navigation(task, plan, attempt, nxt.seq,
                                current, report.to_page, DRIVER_LIVE)
        return task

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
                                "page": first["page"],
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
        with self._lock:
            child.seq = self._next_seq()
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
        # In the task list's order — creation order, the work
        # stranded longest first. (Sorting these rows by their
        # random task ids, as this once did, undid that order
        # and made the read a coin flip.)
        return orphaned

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

    def budget_report(self) -> list[dict]:
        """What each operator's executions cost, folded from the
        attempt records — never from counters, so the read cannot
        drift from the records it summarises. Per directory name:
        the attempts it ran, the distinct tasks those attempts
        belong to, the steps consumed (the attempts' own outcomes,
        the failed step included — it was spent), and the attempts
        the shell stopped for budget. An attempt's executor is the
        name its claim recorded; attempts from before the
        directory round carry none and fold to nobody — the same
        of-their-time limit the operator report states."""
        attempts: list[ExecutionAttempt] = []
        for raw in self.store.list_records("executions"):
            raw = {k: v for k, v in raw.items() if k != "id"}
            attempts.append(ExecutionAttempt(**raw))
        report: list[dict] = []
        for raw in self.list_operators():
            name = operator_named(raw["id"])
            mine = [a for a in attempts
                    if a.executor
                    and operator_named(a.executor) == name]
            report.append({
                "operator": raw["id"],
                "role": raw["role"],
                "active": self.entry_active(raw),
                "attempts_run": len(mine),
                "tasks_executed": len({a.task_id for a in mine}),
                "steps_consumed": sum(len(a.steps) for a in mine),
                "budget_exhaustions": sum(
                    1 for a in mine if a.budget_exhausted),
            })
        return report

    # -- observability ------------------------------------------------
    # The trace, the taxonomy, and the daily digest (round 15):
    # three reads folded from the records by observability.py.
    # The taxonomy label rides every task read as the derived
    # `ending` field; the trace is one task's execution timeline
    # reconstructed from its attempts; the digest is a dated
    # snapshot of one day's fold, stored as a record (kind
    # `digests`, keyed by the date — the records mechanism, so no
    # migration) because re-generating a day must REPLACE the
    # snapshot and bump its version: a fold alone has nowhere to
    # keep either fact. The digest's day scope is the tasks
    # CREATED on the date, reported as they stand when the
    # digest is generated — a digest re-run tomorrow shows
    # yesterday's tasks as they stand tomorrow, at a higher
    # version, which is exactly what the version is for.

    def task_ending(self, task: Task) -> dict:
        """The taxonomy's verdict on one task, folded from its
        trail, its attempts, and its stored verification report
        by the one function (`observability.classify_ending`)."""
        return classify_ending(
            task, self.audit(task.task_id),
            self.get_attempts(task.task_id),
            self._stored_verification(task.task_id))

    def task_view(self, task: Task) -> dict:
        """A task as the API's list reads serve it: the record,
        plus its ending — derived on every read, never stored,
        so the label cannot disagree with the records — and its
        dual-control standing (round 18), derived the same way
        from the plan and the seats standing on it."""
        view = task.model_dump(mode="json")
        view["ending"] = self.task_ending(task)
        view["dual_control"] = self.dual_control_view(task)
        return view

    def task_trace(self, task_id: str) -> dict:
        """The task's execution trace: its attempts in order
        with their events (steps with before/after values,
        navigations where they happened), the budget each
        attempt ran under, and the ending — the taxonomy's
        verdict and the verification summary where one ran.
        Folded from the records; asking changes nothing."""
        task = self._require_task(task_id)
        return build_trace(
            task, self.get_plan(task_id),
            self.get_attempts(task_id),
            self._stored_verification(task_id),
            self.task_ending(task))

    @staticmethod
    def _digest_day(day: str) -> str:
        """Validate a digest date: an ISO calendar date, exactly
        as written (YYYY-MM-DD). Anything else is refused — a
        digest keyed by a date nobody can read back is worse
        than no digest."""
        try:
            parsed = datetime.strptime(day, "%Y-%m-%d")
        except (TypeError, ValueError):
            raise GateError(
                f"'{day}' is not an ISO date (YYYY-MM-DD); a "
                f"digest is keyed by the day it summarises")
        if parsed.strftime("%Y-%m-%d") != day:
            raise GateError(
                f"'{day}' is not an ISO date (YYYY-MM-DD); a "
                f"digest is keyed by the day it summarises")
        return day

    def generate_digest(self, day: str, actor: str) -> dict:
        """Fold one day's work and store the snapshot: the
        tasks created on `day`, as they stand now, counted by
        ending and taxonomy, their proven credits totalled,
        clarifications, budget exhaustions, and discrepancies
        open vs accepted (`observability.fold_digest`). Storing
        replaces any earlier snapshot of the day and bumps its
        version — the snapshot is the day as reported, and a
        re-report is a new version of it, never a quiet edit.
        The record carries its own provenance (generated_at,
        generated_by); generating decides nothing about any
        task, so — like the re-plan and reject refusals — it
        writes no audit entry."""
        self._digest_day(day)
        tasks = [t for t in self.list_tasks()
                 if t.created_at[:10] == day]
        payload = fold_digest(
            day, tasks,
            {t.task_id: self.get_attempts(t.task_id)
             for t in tasks},
            {t.task_id: self._stored_verification(t.task_id)
             for t in tasks},
            {t.task_id: self.audit(t.task_id) for t in tasks},
            {t.task_id: self.dual_control_view(t) for t in tasks})
        previous = self.store.get_record("digests", day)
        record = {
            "id": day, **payload,
            "version": (int(previous.get("version", 0)) + 1
                        if previous else 1),
            "generated_at": _now(),
            "generated_by": actor,
        }
        self.store.upsert_record("digests", record)
        return {k: v for k, v in record.items() if k != "id"}

    def get_digest(self, day: str) -> Optional[dict]:
        """The stored digest for a day, or None when the day
        has not been generated. Reading never generates: a day
        nobody has summarised reads as absent, not as zeros."""
        raw = self.store.get_record("digests", day)
        if raw is None:
            return None
        return {k: v for k, v in raw.items() if k != "id"}

    def list_digests(self) -> list[dict]:
        """Every stored digest, by date."""
        digests = []
        for raw in self.store.list_records("digests"):
            digests.append(
                {k: v for k, v in raw.items() if k != "id"})
        return sorted(digests, key=lambda d: d["date"])

    # -- batches -------------------------------------------------------
    def _batch_refused(self, operator: str, code: str,
                       reason: str) -> None:
        self._audit(BATCH_AUDIT_SCOPE, operator, "batch_refused", {
            "code": code, "reason": reason})

    def create_batch(self, operator: str,
                     members: list[BatchMemberIn]) -> Batch:
        """State a list of work as one batch: each member, in
        submission order, through the ordinary doors — a task
        text through `intake`, a template member through
        `instantiate_template`, which itself renders and intakes.
        The batch adds no gate and bypasses none: the role rules
        and policy apply to every member exactly as they do to a
        task stated on its own, and a member that refuses is IN
        the batch with its refusal — a refused task as a REFUSED
        member, and a template member whose slots do not
        validate as a member carrying instantiation's refusal,
        no task created. The batch record is written once,
        holding what creation produced; it is never edited
        afterwards, and it stores no status — completion is a
        fold over the members, read fresh every time.

        A malformed submission — no members at all, or a member
        naming both a text and a template, or neither — is
        refused whole, recorded, before any member is stated:
        a bad list is not a batch with members silently missing.
        """
        if not members:
            reason = ("a batch names no members; there is "
                      "nothing to submit together")
            self._batch_refused(operator, "no_members", reason)
            raise BatchError("no_members", f"refused: {reason}")
        for position, member in enumerate(members, start=1):
            has_text = bool((member.text or "").strip())
            has_template = bool((member.template or "").strip())
            if has_text == has_template:
                reason = (f"member {position} names "
                          f"{'both a task text and a template' if has_text else 'neither a task text nor a template'}; "
                          f"a member is one or the other")
                self._batch_refused(operator, "member_invalid",
                                    reason)
                raise BatchError("member_invalid",
                                 f"refused: {reason}")
        batch_id = f"BATCH-{uuid.uuid4().hex[:8].upper()}"
        records: list[BatchMemberRecord] = []
        for position, member in enumerate(members, start=1):
            task: Optional[Task] = None
            if (member.text or "").strip():
                task = self.intake(member.text or "", operator)
                record = BatchMemberRecord(
                    position=position, kind="text",
                    task_id=task.task_id, text=task.text)
            else:
                try:
                    task = self.instantiate_template(
                        member.template or "", member.version,
                        dict(member.slots), operator)
                    record = BatchMemberRecord(
                        position=position, kind="template",
                        task_id=task.task_id, text=task.text,
                        template_name=task.template_name,
                        template_version=task.template_version)
                except TemplateError as err:
                    # Instantiation refused before a task
                    # existed — the refusal is already recorded
                    # in the library's scope by instantiate
                    # itself; the member carries it here so the
                    # batch shows the whole submission.
                    record = BatchMemberRecord(
                        position=position, kind="template",
                        template_name=member.template,
                        template_version=member.version,
                        refusal_code=err.code,
                        refusal_reason=err.message)
            if task is not None:
                task.batch_id = batch_id
                self.store.put_task(task.model_dump(mode="json"))
                self._audit(task.task_id, operator, "batch_member", {
                    "batch_id": batch_id, "position": position,
                    "kind": record.kind})
            records.append(record)
        batch = Batch(batch_id=batch_id, created_by=operator,
                      created_at=_now(), members=records)
        with self._lock:
            batch.seq = self._next_seq()
            self.store.upsert_record("batches", {
                "id": batch_id, **batch.model_dump(mode="json")})
        self._audit(BATCH_AUDIT_SCOPE, operator, "batch_created", {
            "batch_id": batch_id,
            "members": [r.model_dump(mode="json") for r in records]})
        return batch

    def get_batch(self, batch_id: str) -> Optional[Batch]:
        raw = self.store.get_record("batches", batch_id)
        if raw is None:
            return None
        raw = {k: v for k, v in raw.items() if k != "id"}
        return Batch(**raw)

    def list_batches(self) -> list[Batch]:
        """Every batch, in submission order — the creation
        sequence the service stamped at first storage. That
        number is the guarantee: `created_at` has one-second
        granularity, so two batches submitted in the same second
        tie on it, and ordering the tie by the random batch id
        made the list a coin flip. Batches stored before the
        sequence existed carry 0 and fall back to (created_at,
        batch_id) behind it."""
        batches = []
        for raw in self.store.list_records("batches"):
            raw = {k: v for k, v in raw.items() if k != "id"}
            batches.append(Batch(**raw))
        return sorted(batches,
                      key=lambda b: (b.seq, b.created_at, b.batch_id))

    def batch_audit(self) -> list[dict]:
        return self.store.list_audit(BATCH_AUDIT_SCOPE)

    def _member_row(self, member: BatchMemberRecord) -> dict:
        """One member as the batch's reads show it: what
        creation recorded, joined with the member task's state
        as it stands NOW — the join is the point, so a sheet
        printed an hour later shows the work as it is, not as
        it was submitted. A member that never became a task
        stands REFUSED with instantiation's reason, terminal,
        owing nothing."""
        row: dict = {
            "position": member.position,
            "kind": member.kind,
            "task_id": member.task_id,
            "text": member.text,
            "template_name": member.template_name,
            "template_version": member.template_version,
        }
        if member.task_id is None:
            row.update({
                "status": TaskStatus.REFUSED.value,
                "terminal": True,
                "refusal_reason": (f"{member.refusal_code}: "
                                   f"{member.refusal_reason}"),
                "next_act": {
                    "action": None,
                    "detail": ("complete — refused before a task "
                               "existed; nothing was stated"),
                    "who": None},
            })
            return row
        task = self.get_task(member.task_id)
        if task is None:  # a stored member naming no task: say so
            row.update({
                "status": "MISSING",
                "terminal": False,
                "refusal_reason": None,
                "next_act": {
                    "action": None,
                    "detail": ("the member names a task the store "
                               "does not hold"),
                    "who": None},
            })
            return row
        row.update({
            "status": task.status.value,
            "terminal": task.status in TERMINAL_STATUSES,
            "operator": task.operator,
            "planned_by": task.planned_by or task.operator,
            "plan_version": task.plan_version,
            "refusal_reason": task.refusal_reason,
            "failed_step": task.failed_step,
            "failure_error": task.failure_error,
            "next_act": next_act_for(
                task, dual=self.dual_control_view(task)),
        })
        return row

    def _derived_status(self, rows: list[dict]) -> str:
        """A batch is complete exactly when every member stands
        in a terminal state; otherwise it is open, and the reads
        name the members still owing acts. Folded from the rows
        on every read — never stored on the batch."""
        return ("complete"
                if all(r["terminal"] for r in rows) else "open")

    def batch_view(self, batch: Batch) -> dict:
        """The batch as the API and console serve it: the stored
        record, its members joined with their live states and
        next acts (the run sheet's rows), and the derived
        status folded over them."""
        rows = [self._member_row(m) for m in batch.members]
        view = batch.model_dump(mode="json")
        view["members"] = rows
        view["status"] = self._derived_status(rows)
        view["members_total"] = len(rows)
        view["members_terminal"] = sum(
            1 for r in rows if r["terminal"])
        return view

    def batch_run_sheet(self, batch: Batch) -> dict:
        """The run sheet: the batch as a work list. Each member
        with its state, the next act it needs, and who may take
        that act — every act named here is taken on the member
        task itself, through the ordinary per-task doors; the
        sheet takes none of them."""
        rows = [self._member_row(m) for m in batch.members]
        return {
            "batch_id": batch.batch_id,
            "created_by": batch.created_by,
            "created_at": batch.created_at,
            "status": self._derived_status(rows),
            "members": rows,
            "owing": [r["task_id"] for r in rows
                      if not r["terminal"]],
        }

    def batch_report(self, batch: Batch) -> dict:
        """The batch report, folded from the member records —
        never from anything the batch stored about outcomes,
        because it stores none. Counts by state; the credited
        total summed from the VERIFIED members' stored
        verification reports (the amount checks the re-read
        passed, never the plans' values — a member whose
        execution landed in DISCREPANCY contributes nothing,
        however good its plan looked); the discrepancies still
        open, with their diffs and correction chains; the
        failures, with their steps and errors; the refusals,
        with their reasons; and whether the batch is complete,
        or the members still owing acts, named."""
        rows = [self._member_row(m) for m in batch.members]
        counts: Counter = Counter(r["status"] for r in rows)
        credited: list[dict] = []
        total = Decimal("0.00")
        discrepancies: list[dict] = []
        failures: list[dict] = []
        refused: list[dict] = []
        owing: list[dict] = []
        for member, row in zip(batch.members, rows):
            if not row["terminal"]:
                owing.append({
                    "task_id": row["task_id"],
                    "position": row["position"],
                    "status": row["status"],
                    "next_act": row["next_act"]["action"]})
            if row["task_id"] is None:
                refused.append({
                    "position": row["position"], "task_id": None,
                    "reason": row["refusal_reason"]})
                continue
            task = self.get_task(row["task_id"])
            if task is None:
                continue
            if task.status == TaskStatus.REFUSED:
                refused.append({
                    "position": row["position"],
                    "task_id": task.task_id,
                    "reason": task.refusal_reason})
            elif task.status == TaskStatus.DISCREPANCY:
                report = self.get_verification(task.task_id)
                discrepancies.append({
                    "task_id": task.task_id,
                    "diffs": ([d.model_dump(mode="json")
                               for d in report.diffs]
                              if report else []),
                    "corrections": (
                        [c.model_dump(mode="json")
                         for c in report.corrections]
                        if report else []),
                })
            elif task.status == TaskStatus.EXECUTION_FAILED:
                failures.append({
                    "task_id": task.task_id,
                    "failed_step": task.failed_step,
                    "failure_error": task.failure_error})
            if task.status == TaskStatus.VERIFIED:
                report = self._stored_verification(task.task_id)
                if report is not None and report.ok:
                    member_total = Decimal("0.00")
                    for check in report.field_checks:
                        if (check.record_kind == "credit_memos"
                                and check.field == "amount"
                                and check.verdict == "match"
                                and check.actual is not None):
                            member_total += Decimal(check.actual)
                    if member_total > 0:
                        credited.append({
                            "task_id": task.task_id,
                            "credited": str(member_total)})
                        total += member_total
        return {
            "batch_id": batch.batch_id,
            "created_by": batch.created_by,
            "created_at": batch.created_at,
            "complete": all(r["terminal"] for r in rows),
            "status": self._derived_status(rows),
            "members": len(rows),
            "counts_by_state": dict(sorted(counts.items())),
            "total_credited": str(total),
            "credited": credited,
            "discrepancies_open": discrepancies,
            "failures": failures,
            "refused": refused,
            "owing_acts": owing,
        }

    # -- audit exports ------------------------------------------------
    # An export takes one scope's trail and chains it (exports.py):
    # every entry's SHA-256 over the previous hash and its own
    # canonical content, under a header naming the scope, the
    # exporter, the export time, the count, and the final hash.
    # Exporting is a recorded act with an identity gate, like
    # every other act in the shell: the exporter must be an
    # enrolled, active operator; a refusal is written into the
    # exported scope's trail before it raises. The act's own
    # entry (`audit_exported`) is written AFTER the document is
    # built, so a document never contains its own export entry —
    # the chain covers the trail as it stood at export, and the
    # next export of the scope chains the earlier export's entry
    # in with the rest.

    # The governance scopes an export can name, by the key the
    # API and the document use.
    EXPORT_SCOPES = {
        "roles": ROLE_AUDIT_SCOPE,
        "templates": TEMPLATE_AUDIT_SCOPE,
    }

    def _export_gate(self, scope_task_id: str, exporter: str,
                     scope: dict) -> None:
        """The identity gate every export passes, mirroring the
        confirmation gate's refusals: an unknown name and a
        deactivated name are each refused, and the refusal is
        recorded in the exported scope's trail, under the name
        that tried, before it raises."""
        entry = self.directory_entry(exporter)
        if entry is not None and not self.entry_active(entry):
            reason = (f"'{exporter}' is deactivated; a "
                      f"deactivated operator may not export "
                      f"audit trails")
            self._audit(scope_task_id, exporter, "export_refused", {
                "code": "operator_inactive", "reason": reason,
                "scope": scope})
            raise GateError(
                f"audit export refused: {reason} "
                f"(operator_inactive)")
        if entry is None:
            reason = (f"'{exporter}' holds no role in the "
                      f"operator directory; an export is an "
                      f"act of a named, enrolled operator")
            self._audit(scope_task_id, exporter, "export_refused", {
                "code": "unknown_operator", "reason": reason,
                "scope": scope})
            raise GateError(
                f"audit export refused: {reason} "
                f"(unknown_operator)")

    def _finish_export(self, scope_task_id: str, scope: dict,
                       exporter: str, entries: list[dict],
                       rendering: str) -> dict:
        """Build the chained document from the trail as it
        stands, then record the act in the scope's trail — in
        that order, so the document never contains its own
        export entry."""
        document = build_export(scope, exporter, _now(), entries)
        self._audit(scope_task_id, exporter, "audit_exported", {
            "scope": scope,
            "entry_count": document["entry_count"],
            "final_hash": document["final_hash"],
            "rendering": rendering})
        return document

    def export_task_audit(self, task_id: str, exporter: str,
                          rendering: str = "json") -> dict:
        """One task's trail, chained. The task must exist; the
        exporter must pass the identity gate."""
        self._require_task(task_id)
        scope = {"kind": "task", "id": task_id}
        self._export_gate(task_id, exporter, scope)
        return self._finish_export(
            task_id, scope, exporter, self.audit(task_id),
            rendering)

    def export_batch_audit(self, batch_id: str, exporter: str,
                           rendering: str = "json") -> dict:
        """One batch's trail, chained: the batch scope's own
        entries that name this batch, and every member task's
        trail, interleaved in the trail's true order — the
        global audit read filtered to the batch, never a
        per-task concatenation that would invent an order the
        work did not happen in. A member that never became a
        task has no trail of its own; the batch scope's
        `batch_created` entry carries its refusal."""
        batch = self.get_batch(batch_id)
        if batch is None:
            raise KeyError(f"batch {batch_id} does not exist")
        scope = {"kind": "batch", "id": batch_id}
        self._export_gate(BATCH_AUDIT_SCOPE, exporter, scope)
        member_ids = {m.task_id for m in batch.members if m.task_id}

        def names_this_batch(entry: dict) -> bool:
            detail = entry.get("detail") or {}
            if detail.get("batch_id") == batch_id:
                return True
            # The export acts name their scope the document's
            # way, not the creation entry's way — a later batch
            # export chains an earlier one's act in, exactly as
            # a task export chains its predecessor.
            scope = detail.get("scope") or {}
            return (scope.get("kind") == "batch"
                    and scope.get("id") == batch_id)

        entries = [
            e for e in self.store.list_all_audit()
            if e["task_id"] in member_ids
            or (e["task_id"] == BATCH_AUDIT_SCOPE
                and names_this_batch(e))]
        return self._finish_export(
            BATCH_AUDIT_SCOPE, scope, exporter, entries, rendering)

    def export_scope_audit(self, scope_name: str, exporter: str,
                           rendering: str = "json") -> dict:
        """One governance scope's trail, chained — `roles` or
        `templates`, the scopes whose acts administer the shell
        itself (role edits, directory acts, template publishes
        and refusals)."""
        scope_task_id = self.EXPORT_SCOPES.get(scope_name)
        if scope_task_id is None:
            raise GateError(
                f"there is no exportable audit scope named "
                f"'{scope_name}'")
        scope = {"kind": scope_name}
        self._export_gate(scope_task_id, exporter, scope)
        return self._finish_export(
            scope_task_id, scope, exporter,
            self.store.list_audit(scope_task_id), rendering)

    @staticmethod
    def render_export_csv(document: dict) -> str:
        return render_csv(document)

    @staticmethod
    def verify_audit_export(document: dict) -> dict:
        """Grade an export document's chain — a read, not an
        act: nothing is recorded, because checking evidence
        must not write on the trail it checks."""
        return verify_export(document)
