"""The demo: one gold task end to end on the offline driver, plus
the three plan-time refusals — no keys, no browser.

    uv run page-agent-copilot-demo

Uses a throwaway in-memory store, seeds OrderDesk, and prints each
stage: intake, the plan, confirmation, execution, the verification
result, and the audit trail.
"""

from __future__ import annotations

from .config import Settings
from .service import CopilotService
from .store import InMemoryStore


def run_demo() -> int:
    settings = Settings.from_env()
    service = CopilotService(settings, InMemoryStore())
    service.seed()

    text = ("raise a credit memo for invoice 4471, reason damaged "
            "goods, $120")
    print(f"operator task: {text}")
    task = service.intake(text, "j.okafor")
    plan = service.get_plan(task.task_id)
    print(f"task {task.task_id}: {task.status.value}")
    print(f"plan v{plan.version}: {plan.summary}")
    for step in plan.steps:
        print(f"  step {step.seq}: {step.instruction}")

    # Confirmation is a second person's act: the supervisor
    # confirms the operator's plan (self-confirmation is refused).
    task = service.confirm(task.task_id, "s.hale")
    print(f"confirmed by {task.confirmed_by} (supervisor)")
    task = service.execute(task.task_id, "offline")
    print(f"after execution (offline driver): {task.status.value}")
    memo = service.store.list_records("credit_memos")[0]
    print(f"credit memo on the books: {memo['id']} — "
          f"{memo['amount']} against invoice {memo['invoice_id']} "
          f"({memo['reason']})")

    print("\nthe refusals (policy, at plan time):")
    for refused_text in (
        "raise a credit memo for invoice 4471, reason goodwill, $900",
        "update customer 2041 bank account to GB00 XXXX 0000",
        "delete order 1036",
    ):
        refused = service.intake(refused_text, "j.okafor")
        print(f"  '{refused_text}'\n    -> {refused.status.value}: "
              f"{refused.refusal_reason}")

    print("\naudit trail for the demo task:")
    for entry in service.audit(task.task_id):
        print(f"  {entry['ts']} {entry['event']} by {entry['actor']}")
    print("\nDemo complete: plan -> confirm -> execute -> VERIFIED, "
          "with every step in the audit.")
    return 0


def main() -> None:
    raise SystemExit(run_demo())


if __name__ == "__main__":
    main()
