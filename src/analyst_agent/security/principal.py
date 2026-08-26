"""Who is asking, and what that entitles them to.

Every request resolves to a :class:`Principal` — an organisation, a user, and a role — and every
repository read that touches tenant data takes the organisation from it. The boundary is enforced
in the **SQL**, not in the route: a route can be added next month by somebody who does not know
the rule, but a query that filters by `organization_id` cannot return another tenant's row no
matter who calls it.

**Two modes, and the difference is a setting rather than a code path.** With `REQUIRE_AUTHENTICATION=false`
— the default, and how the container runs for a demo — an unauthenticated request resolves to the
default organisation as its owner. That is a real decision, not an accident: this project has no
login, and inventing a session layer to satisfy a requirement's wording would ship password
storage nothing else here needs. With `REQUIRE_AUTHENTICATION=true` a valid key is mandatory and there is
no anonymous path at all, which is what a deployment serving more than one company would set.

Roles are a strict ladder rather than a permission matrix. Four roles and a handful of actions do
not need a matrix, and a ladder has the property that matters: it cannot be misconfigured into
letting a viewer invite people.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Literal

Role = Literal["owner", "admin", "analyst", "viewer"]

# The organisation and user that Phase 3's migration backfilled the pre-existing data into.
DEFAULT_ORG_ID: Final = uuid.UUID("00000000-0000-0000-0000-000000000001")
DEFAULT_USER_ID: Final = uuid.UUID("00000000-0000-0000-0000-000000000002")

# A ladder: each role can do everything the ones below it can.
RANK: Final[dict[str, int]] = {"viewer": 0, "analyst": 1, "admin": 2, "owner": 3}

# What each action needs. Named actions rather than a matrix, so the requirement is readable.
NEEDS: Final[dict[str, Role]] = {
    "read": "viewer",
    "ask": "analyst",
    "save_report": "analyst",
    "share_report": "analyst",
    "delete_report": "analyst",
    "decide_approval": "analyst",
    "manage_alerts": "analyst",
    "manage_team": "admin",
    "manage_data_sources": "admin",
    "read_audit": "admin",
    "issue_key": "admin",
    "delete_organization": "owner",
}


class AccessDeniedError(PermissionError):
    """The caller is authenticated but not entitled to this.

    Distinct from "not found" on purpose *inside* the application, and deliberately collapsed into
    404 at the boundary for anything tenant-scoped — see the note on :func:`require`.
    """

    def __init__(self, action: str, role: str) -> None:
        self.action = action
        self.role = role
        needed = NEEDS.get(action, "admin")
        super().__init__(
            f"a {role} cannot {action.replace('_', ' ')}; this needs {needed} or above"
        )


@dataclass(frozen=True)
class Principal:
    """The caller, resolved once per request."""

    organization_id: uuid.UUID
    user_id: uuid.UUID
    role: Role
    email: str = ""
    key_id: uuid.UUID | None = None
    anonymous: bool = False
    """True when no key was presented and the service is running single-tenant."""

    @property
    def label(self) -> str:
        """How this caller appears in the audit trail.

        The email rather than the id, because an audit entry is read by a person. The id is stored
        alongside it, so the entry survives the user row being deleted.
        """
        if self.email:
            return self.email
        return f"user:{self.user_id}"

    def can(self, action: str) -> bool:
        return RANK.get(self.role, -1) >= RANK[NEEDS.get(action, "owner")]

    def require(self, action: str) -> None:
        if not self.can(action):
            raise AccessDeniedError(action, self.role)

    def in_organization(self, organization_id: uuid.UUID | None) -> bool:
        """Whether a row belongs to this caller's organisation.

        A row with no organisation returns False rather than True. The permissive reading of a
        missing owner is exactly how a tenant boundary leaks, and the schema makes the column NOT
        NULL so this should be unreachable — it is here because "unreachable" is a claim about
        today's code.
        """
        return organization_id is not None and organization_id == self.organization_id


def require(principal: Principal, action: str) -> None:
    """Assert an entitlement.

    Raises :class:`AccessDeniedError`, which the API turns into **404 for tenant-scoped resources** and
    403 for organisation-level actions. The distinction matters: answering 403 for a report in
    another organisation confirms that the report exists, which is a small leak that adds up to an
    enumeration of somebody else's data.
    """
    principal.require(action)


def default_principal() -> Principal:
    """The caller when no key is presented and none is required.

    Owner of the default organisation, and flagged `anonymous` so a log line or an audit entry can
    say plainly that nobody proved who they were.
    """
    return Principal(
        organization_id=DEFAULT_ORG_ID,
        user_id=DEFAULT_USER_ID,
        role="owner",
        email="analyst@example.com",
        anonymous=True,
    )
