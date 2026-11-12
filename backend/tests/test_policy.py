import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.policy import BuyerPolicy, Condition, evaluate_condition

POLICIES = Path(__file__).resolve().parents[1] / "policies"


def load(name):
    return json.loads((POLICIES / name).read_text(encoding="utf-8"))


def test_shipped_policies_validate():
    for name in ("buyer_a.json", "buyer_b.json"):
        BuyerPolicy.model_validate(load(name))


def test_unknown_field_rejected():
    p = load("buyer_a.json")
    p["required_fields"].append({"field": "business.favourite_colour"})
    with pytest.raises(ValidationError, match="unknown required field"):
        BuyerPolicy.model_validate(p)


def test_unknown_document_type_rejected():
    p = load("buyer_a.json")
    p["documents"].append({"id": "x", "document_type": "aadhaar", "level": "required", "reason": "?"})
    with pytest.raises(ValidationError, match="unknown document type"):
        BuyerPolicy.model_validate(p)


def test_contradictory_requirement_rejected():
    p = load("buyer_a.json")
    p["documents"].append({"id": "pan2", "document_type": "pan_card", "level": "optional", "reason": "?"})
    with pytest.raises(ValidationError, match="both required and optional"):
        BuyerPolicy.model_validate(p)


def test_simulated_evidence_only_in_test_policy():
    p = copy.deepcopy(load("buyer_a.json"))
    p["is_test_policy"] = False
    with pytest.raises(ValidationError, match="only a test policy"):
        BuyerPolicy.model_validate(p)


def test_unknown_condition_input_is_unresolved_not_false():
    c = Condition(field="tax.msme_registered", op="eq", value="yes")
    assert evaluate_condition(c, {"tax": {}}) is None
    assert evaluate_condition(c, {"tax": {"msme_registered": "no"}}) is False
    assert evaluate_condition(c, {"tax": {"msme_registered": "yes"}}) is True
