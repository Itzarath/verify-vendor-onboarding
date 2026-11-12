"""Idempotent seed: two fictional buyers with their policies, and reviewer / finance accounts."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from .config import BACKEND_ROOT
from .db import session_scope
from .models import Buyer, Policy, User
from .schemas.policy import BuyerPolicy

BUYERS = [
    {"slug": "meridian", "name": "Meridian Retail India (fictional)", "policy": "buyer_a.json",
     "users": [("priya.sharma@meridian.example", "Priya Sharma — Procurement reviewer", "reviewer"),
               ("farah.khan@meridian.example", "Farah Khan — Finance reviewer", "finance"),
               ("arjun.iyer@meridian.example", "Arjun Iyer — Procurement reviewer", "reviewer")]},
    {"slug": "kestrel", "name": "Kestrel Health Systems (fictional)", "policy": "buyer_b.json",
     "users": [("rahul.menon@kestrel.example", "Rahul Menon — Procurement reviewer", "reviewer"),
               ("nisha.rao@kestrel.example", "Nisha Rao — Finance reviewer", "finance"),
               ("meera.pillai@kestrel.example", "Meera Pillai — Procurement reviewer", "reviewer")]},
]
# Two procurement reviewers per buyer: a high-risk supplier needs two different approvers, and whoever confirmed a bank
# change can't be one of them (commands.approve), so finance alone can't be the second.


def _same_policy(stored: dict, current: dict) -> bool:
    """Does a stored policy document mean the same as the file, under the current schema?

    A stored document is never rewritten: requests are pinned to it. But a newer schema can add a setting with a default
    (e.g. `approval`, which stored documents from before it was added don't contain). Such a document still means the
    same policy, so it is compared after reading it with the current schema; any real difference is still refused."""
    try:
        return BuyerPolicy.model_validate(stored).model_dump(mode="json") == current
    except ValueError:
        return False


def seed() -> None:
    with session_scope() as db:
        for b in BUYERS:
            buyer = db.scalar(select(Buyer).where(Buyer.slug == b["slug"]))
            if buyer is None:
                buyer = Buyer(slug=b["slug"], name=b["name"])
                db.add(buyer)
                db.flush()
            raw = json.loads((BACKEND_ROOT / "policies" / b["policy"]).read_text(encoding="utf-8"))
            pol_model = BuyerPolicy.model_validate(raw)  # refuse to activate an invalid policy
            doc = pol_model.model_dump(mode="json")
            sha = hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()
            pol = db.scalar(select(Policy).where(Policy.buyer_id == buyer.id, Policy.version == pol_model.version))
            if pol is None:
                pol = Policy(buyer_id=buyer.id, policy_key=pol_model.policy_key, version=pol_model.version, document=doc, sha256=sha)
                db.add(pol)
                db.flush()
            elif pol.sha256 != sha and not _same_policy(pol.document, doc):
                raise RuntimeError(f"Policy {pol_model.policy_key} v{pol_model.version} changed on disk; bump its version "
                                   "instead of editing an active version in place")
            buyer.active_policy_id = pol.id
            for email, name, role in b["users"]:
                if db.scalar(select(User).where(User.email == email)) is None:
                    db.add(User(email=email, display_name=name, role=role, buyer_id=buyer.id))


if __name__ == "__main__":
    seed()
    print("seeded")
