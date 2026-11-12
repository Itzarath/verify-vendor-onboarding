"""Stage 5 — decide what happens next; Stage 6 — what to tell whom.

Routing (docs/ARCHITECTURE.md, "Routing and states"):
1. A configured automatic-rejection rule with reliable evidence  -> REJECTED
2. Any blocking finding unresolved (missing/failed/uncertain/awaiting review) -> ACTION_REQUIRED
3. Otherwise -> READY_FOR_DECISION (a reviewer still makes the final approval)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import RequestState
from .checks import BLOCKING_STATUSES, CheckOutcome

OWNER_KIND = {"vendor": "vendor_action", "reviewer": "review", "finance": "finance_review", "system": "system_retry"}

# What the vendor is told about internal work, without exposing internal signals.
VENDOR_FACING_INTERNAL = {
    "finance:bank_change": "Your updated bank details are being reviewed by our finance team.",
    "reviewer:bank_holder": "We're reviewing the name on your bank account against your registration documents.",
    "reviewer:registry": "We're verifying your GST registration with the official registry.",
    "reviewer:duplicate_bank": "We're carrying out additional checks on your bank details.",
    "reviewer:duplicate_identity": "We're checking your registration details against our existing vendor records.",
    "reviewer:bank_proof_date": "We're checking the date on your bank proof.",
    "system:reading": "Some documents are still being processed on our side — no action needed from you.",
    "system:registry": "We're waiting on the GST registry check (a simulated registry in this demo) — no action needed from you.",
    "reviewer:confirm_reading": "We're checking some values read from your scanned documents — no action needed from you.",
}
GENERIC_INTERNAL = "Our team is reviewing some details of your application."


@dataclass
class TaskSpec:
    task_key: str
    owner: str
    kind: str
    title: str
    lines: list[str] = field(default_factory=list)
    vendor_lines: list[str] = field(default_factory=list)
    check_keys: list[str] = field(default_factory=list)
    fingerprint: str | None = None

    @property
    def detail(self) -> str:
        return "\n".join(dict.fromkeys(self.lines))


@dataclass
class Routing:
    outcome: str  # auto_reject | action_required | ready
    state: str
    headline: str
    blockers: list[CheckOutcome]
    auto_reject: CheckOutcome | None
    tasks: dict[str, TaskSpec]


def route(checks: list[CheckOutcome]) -> Routing:
    auto = next((c for c in checks if c.auto_reject), None)
    blockers = [c for c in checks if c.blocking and c.status in BLOCKING_STATUSES]
    tasks: dict[str, TaskSpec] = {}
    for c in blockers:
        if not c.task_key or c.auto_reject:
            continue
        t = tasks.get(c.task_key)
        if t is None:
            t = tasks[c.task_key] = TaskSpec(c.task_key, c.owner or "reviewer", OWNER_KIND.get(c.owner or "reviewer", "review"),
                                             c.task_title or c.title, fingerprint=c.fingerprint)
        t.lines.append(c.reason)
        if c.vendor_text:
            t.vendor_lines.append(c.vendor_text)
        t.check_keys.append(c.key)
    if auto:
        return Routing("auto_reject", RequestState.REJECTED, f"Rejected by policy rule: {auto.title}", blockers, auto, {})
    if blockers:
        owners = sorted({t.owner for t in tasks.values()})
        return Routing("action_required", RequestState.ACTION_REQUIRED,
                       f"{len(blockers)} item(s) block approval; waiting on {', '.join(owners) or 'review'}", blockers, None, tasks)
    return Routing("ready", RequestState.READY_FOR_DECISION, "All required checks satisfied — ready for final approval", [], None, {})


@dataclass
class VendorMessage:
    kind: str
    subject: str
    body: str
    items: list[dict]


def compose_vendor_message(routing: Routing, legal_name: str | None, revision_number: int,
                           newly_resolved: list[str]) -> VendorMessage:
    name = legal_name or "your business"
    accepted = [{"type": "accepted", "text": t} for t in newly_resolved]
    if routing.outcome == "auto_reject":
        a = routing.auto_reject
        return VendorMessage("rejected", "Your application was not accepted",
                             f"We can't onboard {name}: {a.reason if a else 'a policy requirement was not met'} "
                             "If you believe this is a mistake, please contact our procurement team.", [])
    vendor_items, internal = [], []
    for t in routing.tasks.values():
        if t.owner == "vendor":
            lines = list(dict.fromkeys(t.vendor_lines or [t.title]))
            vendor_items.append({"type": "action", "task_key": t.task_key, "title": t.title, "text": lines[0],
                                 "details": lines[1:]})
        else:
            internal.append(VENDOR_FACING_INTERNAL.get(t.task_key, GENERIC_INTERNAL))
    internal_items = [{"type": "info", "text": x} for x in dict.fromkeys(internal)]
    if routing.outcome == "ready":
        return VendorMessage("under_review", "Checks complete — awaiting final approval",
                             f"Thanks — revision {revision_number} of the application for {name} has no open items: our "
                             "checks and reviews are complete. A member of our procurement team will make the final "
                             "decision shortly. No action is needed from you.",
                             accepted)
    if vendor_items:
        n = len(vendor_items)
        return VendorMessage("action_required", f"Action needed: {n} item{'s' if n > 1 else ''} to fix",
                             f"We've reviewed revision {revision_number} of the application for {name}. "
                             "Please fix the items below and resubmit — you only need to change what's listed."
                             + (" Other parts of your application are being reviewed in the meantime." if internal_items else ""),
                             accepted + vendor_items + internal_items)
    return VendorMessage("under_review", "Your application is under review",
                         f"Revision {revision_number} of the application for {name} has been received and checked. "
                         "No action is needed from you at this time.", accepted + internal_items)
