"""Buyer policy schema: what a buyer can configure, validated before activation.

The JSON configures capabilities the engine already supports. It cannot add
arbitrary code, prompts, roles or workflow transitions.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .submission import DOCUMENT_TYPES, KNOWN_FIELDS, field_type


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Condition(_M):
    field: str
    op: Literal["eq", "ne", "in"]
    value: Any

    @model_validator(mode="after")
    def _known(self):
        if self.field not in KNOWN_FIELDS:
            raise ValueError(f"unknown field in condition: {self.field}")
        if self.op == "in" and not isinstance(self.value, list):
            raise ValueError("'in' needs a list value")
        # The compared value(s) must be something the form field can actually hold, or the condition silently never fires.
        kind, choices = field_type(self.field)
        values = self.value if self.op == "in" else [self.value]
        for v in values:
            if choices is not None and v not in choices:
                raise ValueError(f"condition on {self.field}: {v!r} is not a possible answer (allowed: {sorted(choices)})")
            if choices is None and kind is bool and not isinstance(v, bool):
                raise ValueError(f"condition on {self.field}: expected true/false, got {v!r}")
            if choices is None and kind is str and not isinstance(v, str):
                raise ValueError(f"condition on {self.field}: expected text, got {v!r}")
        return self


class FieldRequirement(_M):
    field: str
    when: Condition | None = None

    @model_validator(mode="after")
    def _known(self):
        if self.field not in KNOWN_FIELDS:
            raise ValueError(f"unknown required field: {self.field}")
        return self


class DocumentRequirement(_M):
    id: str
    document_type: str
    level: Literal["required", "optional"]
    when: Condition | None = None
    reason: str

    @model_validator(mode="after")
    def _known(self):
        if self.document_type not in DOCUMENT_TYPES:
            raise ValueError(f"unknown document type: {self.document_type}")
        return self


class DeclarationRules(_M):
    conduct_declined: Literal["auto_reject", "review"] = "auto_reject"
    conduct_unanswered: Literal["request_information"] = "request_information"
    conflict_disclosed: Literal["review"] = "review"


# Verification capabilities the engine actually implements. Anything else is refused at activation,
# so a policy can never advertise a check that would silently be skipped.
SUPPORTED_VERIFICATIONS = {"GSTIN"}


class VerificationRequirement(_M):
    id: str
    identifier: Literal["GSTIN", "REGISTRATION"]  # REGISTRATION = CIN or LLPIN (not implemented yet)
    required: bool = True
    accepted_methods: list[Literal["simulated_registry", "manual_registry"]]

    @model_validator(mode="after")
    def _supported(self):
        if self.identifier not in SUPPORTED_VERIFICATIONS:
            raise ValueError(f"{self.id}: {self.identifier} registry verification is not implemented by this engine; "
                             "remove it or implement it before activating the policy")
        if not self.accepted_methods:
            raise ValueError(f"{self.id}: at least one accepted method is needed")
        return self


class ReviewRules(_M):
    bank_change_requires_finance_review: bool = True
    duplicate_bank_account: Literal["review"] = "review"
    gst_state_differs_from_address: Literal["review", "ignore"] = "review"


class ApprovalRules(_M):
    # Approve a request automatically when its evaluation is clean and no person's business judgement was ever needed
    # on it (see runner.auto_approval_block). Off by default; refused on non-test policies because no supported real
    # automated verification exists yet (the simulated registry is a test adapter; manual lookups are a person).
    auto_approve_when_clean: bool = False
    # Risk-based routing (pipeline/risk.py): a clean request is approved automatically only up to this risk tier, and
    # from this tier on a request needs two different people to approve it ("never": one approver is enough). Where
    # the two overlap, two people win: automatic approval never applies from second_approver_from_risk on.
    auto_approve_max_risk: Literal["low", "medium", "high"] = "low"
    second_approver_from_risk: Literal["medium", "high", "never"] = "high"


class ScopeRules(_M):
    supported_entity_types: list[Literal["private_limited", "llp"]] = ["private_limited", "llp"]
    gst_unregistered: Literal["scope_review"] = "scope_review"


class BuyerPolicy(_M):
    policy_key: str
    version: int
    buyer_name: str
    label: str
    is_test_policy: bool = Field(description="Synthetic demo policy; only test policies may accept simulated evidence")
    scope: ScopeRules = ScopeRules()
    required_fields: list[FieldRequirement]
    documents: list[DocumentRequirement]
    declarations: DeclarationRules = DeclarationRules()
    verification: list[VerificationRequirement] = []
    reviews: ReviewRules = ReviewRules()
    approval: ApprovalRules = ApprovalRules()

    @model_validator(mode="after")
    def _consistent(self):
        ids = [d.id for d in self.documents] + [v.id for v in self.verification]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate requirement ids: {sorted(dupes)}")
        unconditional: dict[str, str] = {}
        for d in self.documents:
            if d.when is None:
                prev = unconditional.get(d.document_type)
                if prev and prev != d.level:
                    raise ValueError(f"{d.document_type} is unconditionally both required and optional")
                unconditional[d.document_type] = d.level
        for v in self.verification:
            if "simulated_registry" in v.accepted_methods and not self.is_test_policy:
                raise ValueError(f"{v.id}: only a test policy may accept simulated_registry evidence")
        if self.approval.auto_approve_when_clean and not self.is_test_policy:
            raise ValueError("automatic approval needs a supported real automated verification; only a test policy "
                             "may enable approval.auto_approve_when_clean")
        return self


def evaluate_condition(cond: Condition, form: dict) -> bool | None:
    """True/False, or None when the triggering answer is unknown (never assume False)."""
    from .submission import get_field

    actual = get_field(form, cond.field)
    if actual is None:
        return None
    if cond.op == "eq":
        return actual == cond.value
    if cond.op == "ne":
        return actual != cond.value
    return actual in cond.value
