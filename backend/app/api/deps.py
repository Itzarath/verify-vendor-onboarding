from __future__ import annotations

from fastapi import Depends, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import User
from ..services.commands import ApiError

COOKIE = "vob_session"
MAX_AGE = 60 * 60 * 24 * 14


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt="vob-session")


def make_session(user_id: int) -> str:
    return _signer().dumps({"uid": user_id})


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    raw = request.cookies.get(COOKIE)
    if not raw:
        raise ApiError(401, "signed_out", "Please sign in")
    try:
        data = _signer().loads(raw, max_age=MAX_AGE)
    except BadSignature:
        raise ApiError(401, "signed_out", "Your session expired; please sign in again")
    user = db.get(User, data.get("uid"))
    if user is None:
        raise ApiError(401, "signed_out", "Please sign in")
    # Each tab says which account it is showing. Tabs of one browser share the cookie, so after a sign-in as someone
    # else in another tab, a click in this one would act as the new account (e.g. file supplier A's draft under B).
    shown = request.headers.get("x-signed-in-as")
    if shown and shown != str(user.id):
        raise ApiError(409, "signed_in_elsewhere", "You signed in as a different account in another tab, so this tab "
                       "can't save anything. Reload the page to continue as that account.")
    return user


def require_vendor(user: User = Depends(current_user)) -> User:
    if user.role != "vendor" or not user.vendor_id:
        raise ApiError(403, "vendors_only", "This action is for vendor accounts")
    return user


def require_reviewer(user: User = Depends(current_user)) -> User:
    if user.role not in ("reviewer", "finance"):
        raise ApiError(403, "reviewers_only", "This action is for the buyer's review team")
    return user
