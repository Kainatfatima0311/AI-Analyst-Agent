"""Repository functions for everything Phase 3 added.

Separate from ``repository.py`` because the two answer different questions. That module is about
one run's trace; this one is about *who* may see it. Keeping them apart means the tenant filter is
visible as a subject in its own right rather than scattered through three hundred lines of trace
queries.

**Every read here takes an organisation.** Not "most", and not "by convention": a query that does
not filter by ``organization_id`` cannot be trusted to be called only from a route that does. The
few functions that deliberately look across organisations — resolving an API key, resolving a
share token — say so in their docstring and are the two places to read carefully.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from analyst_agent.db.engine import rw_conn
from analyst_agent.observability.logging import get_logger
from analyst_agent.security import crypto
from analyst_agent.security.principal import Principal, Role

log = get_logger(__name__)


def _slug(name: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in name.strip()]
    slug = "".join(keep).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:60] or "org"


# --- organisations and people -------------------------------------------------


def create_organization(name: str, owner_email: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A new organisation and its first owner, in one transaction.

    One transaction because an organisation with no members is unreachable — nobody can be invited
    to it, since inviting requires being in it. Creating the pair separately would leave that state
    possible whenever the second statement failed.
    """
    organization_id = uuid.uuid4()
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent.organizations (organization_id, name, slug) VALUES (%s, %s, %s)",
            (organization_id, name.strip(), f"{_slug(name)}-{str(organization_id)[:8]}"),
        )
        user_id = _upsert_user(cur, owner_email)
        cur.execute(
            "INSERT INTO agent.organization_members (organization_id, user_id, role) "
            "VALUES (%s, %s, 'owner')",
            (organization_id, user_id),
        )
    log.info("organization created", organization_id=str(organization_id), name=name.strip())
    return organization_id, user_id


def _upsert_user(cur: Any, email: str, display_name: str | None = None) -> uuid.UUID:
    """Find or create a user by email.

    Email is the identity here because there is no login: a person is the address an invitation
    was sent to. `ON CONFLICT` rather than select-then-insert, so two simultaneous invitations to
    the same address cannot create two users.
    """
    user_id = uuid.uuid4()
    cur.execute(
        "INSERT INTO agent.users (user_id, email, display_name) VALUES (%s, %s, %s) "
        "ON CONFLICT (email) DO UPDATE SET display_name = "
        "COALESCE(EXCLUDED.display_name, agent.users.display_name) "
        "RETURNING user_id",
        (user_id, email.strip().lower(), display_name),
    )
    row = cur.fetchone()
    return uuid.UUID(str(row["user_id"]))


def get_organization(organization_id: uuid.UUID) -> dict[str, Any] | None:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT o.*, (SELECT count(*) FROM agent.organization_members m "
            "             WHERE m.organization_id = o.organization_id) AS members "
            "FROM agent.organizations o WHERE o.organization_id = %s",
            (organization_id,),
        )
        return cur.fetchone()


def list_members(organization_id: uuid.UUID) -> list[dict[str, Any]]:
    """The team, with each member's activity.

    The activity counts come from the same query rather than a second round trip: a team page that
    fetched them separately would show a member count from one moment and their run counts from
    another.
    """
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT m.user_id, m.role, m.created_at AS joined_at, u.email, u.display_name, "
            "       inviter.email AS invited_by_email, "
            "       (SELECT count(*) FROM agent.runs r "
            "        WHERE r.organization_id = m.organization_id "
            "          AND r.requested_by = u.email) AS analyses, "
            "       (SELECT max(r.created_at) FROM agent.runs r "
            "        WHERE r.organization_id = m.organization_id "
            "          AND r.requested_by = u.email) AS last_active_at, "
            "       (SELECT count(*) FROM agent.reports rep "
            "        WHERE rep.organization_id = m.organization_id "
            "          AND rep.saved_by_user_id = m.user_id) AS reports "
            "FROM agent.organization_members m "
            "JOIN agent.users u ON u.user_id = m.user_id "
            "LEFT JOIN agent.users inviter ON inviter.user_id = m.invited_by "
            "WHERE m.organization_id = %s "
            "ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 "
            "                    WHEN 'analyst' THEN 2 ELSE 3 END, u.email",
            (organization_id,),
        )
        return list(cur.fetchall())


def invite_member(
    organization_id: uuid.UUID, email: str, role: Role, invited_by: uuid.UUID | None
) -> tuple[uuid.UUID, bool]:
    """Add a person to an organisation. Returns ``(user_id, created)``.

    Idempotent on the membership: inviting somebody who is already a member updates their role
    rather than failing, because "invite" is what an admin means when they want that person to have
    access, and refusing the second attempt tells them nothing useful.
    """
    with rw_conn() as conn, conn.cursor() as cur:
        user_id = _upsert_user(cur, email)
        cur.execute(
            "INSERT INTO agent.organization_members (organization_id, user_id, role, invited_by) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (organization_id, user_id) DO UPDATE SET role = EXCLUDED.role "
            "RETURNING (xmax = 0) AS inserted",
            (organization_id, user_id, role, invited_by),
        )
        row = cur.fetchone() or {}
    created = bool(row.get("inserted"))
    log.info(
        "member invited",
        organization_id=str(organization_id),
        email=email.strip().lower(),
        role=role,
        created=created,
    )
    return user_id, created


def member(organization_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, Any] | None:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT m.*, u.email FROM agent.organization_members m "
            "JOIN agent.users u ON u.user_id = m.user_id "
            "WHERE m.organization_id = %s AND m.user_id = %s",
            (organization_id, user_id),
        )
        return cur.fetchone()


def count_owners(organization_id: uuid.UUID) -> int:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM agent.organization_members "
            "WHERE organization_id = %s AND role = 'owner'",
            (organization_id,),
        )
        return int((cur.fetchone() or {}).get("n", 0))


def set_member_role(organization_id: uuid.UUID, user_id: uuid.UUID, role: Role) -> bool:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.organization_members SET role = %s "
            "WHERE organization_id = %s AND user_id = %s",
            (role, organization_id, user_id),
        )
        return cur.rowcount == 1


def remove_member(organization_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Remove somebody from an organisation.

    The user row survives: they may belong to another organisation, and their name still appears in
    audit entries and on reports they saved. Membership is the thing being revoked, not the person.
    """
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM agent.organization_members WHERE organization_id = %s AND user_id = %s",
            (organization_id, user_id),
        )
        removed = cur.rowcount == 1
        if removed:
            # Their keys stop working immediately. Leaving a key live for somebody who has been
            # removed is the whole point of being able to remove them.
            cur.execute(
                "UPDATE agent.api_keys SET revoked_at = now() "
                "WHERE organization_id = %s AND user_id = %s AND revoked_at IS NULL",
                (organization_id, user_id),
            )
    return removed


# --- API keys -----------------------------------------------------------------


def issue_api_key(
    organization_id: uuid.UUID, user_id: uuid.UUID, name: str
) -> tuple[uuid.UUID, str]:
    """Mint a key. Returns ``(key_id, token)``; the token is never recoverable afterwards."""
    token, token_hash, prefix = crypto.new_token(crypto.KEY_PREFIX)
    key_id = uuid.uuid4()
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent.api_keys (key_id, organization_id, user_id, name, key_hash, prefix)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (key_id, organization_id, user_id, name.strip(), token_hash, prefix),
        )
    log.info("api key issued", key_id=str(key_id), organization_id=str(organization_id))
    return key_id, token


def resolve_api_key(token: str) -> Principal | None:
    """Turn a presented key into a caller.

    **One of the two functions here that looks across organisations**, because it is the function
    that decides which organisation the request is in. It matches on the hash of the presented
    value, so a key cannot be recovered from the database, and it refuses a key whose membership
    has since been removed even if the key row itself was somehow left live.
    """
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT k.key_id, k.organization_id, k.user_id, u.email, m.role "
            "FROM agent.api_keys k "
            "JOIN agent.users u ON u.user_id = k.user_id "
            "JOIN agent.organization_members m ON m.organization_id = k.organization_id "
            "                                AND m.user_id = k.user_id "
            "WHERE k.key_hash = %s AND k.revoked_at IS NULL",
            (crypto.hash_token(token),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "UPDATE agent.api_keys SET last_used_at = now() WHERE key_id = %s", (row["key_id"],)
        )
    return Principal(
        organization_id=uuid.UUID(str(row["organization_id"])),
        user_id=uuid.UUID(str(row["user_id"])),
        role=row["role"],
        email=row["email"],
        key_id=uuid.UUID(str(row["key_id"])),
    )


def revoke_api_key(organization_id: uuid.UUID, key_id: uuid.UUID) -> bool:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.api_keys SET revoked_at = now() "
            "WHERE key_id = %s AND organization_id = %s AND revoked_at IS NULL",
            (key_id, organization_id),
        )
        return cur.rowcount == 1


def list_api_keys(organization_id: uuid.UUID) -> list[dict[str, Any]]:
    """Keys, by prefix. The hash is never selected — there is nothing a caller could do with it."""
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT k.key_id, k.name, k.prefix, k.created_at, k.last_used_at, k.revoked_at, "
            "       u.email FROM agent.api_keys k JOIN agent.users u ON u.user_id = k.user_id "
            "WHERE k.organization_id = %s ORDER BY k.created_at DESC",
            (organization_id,),
        )
        return list(cur.fetchall())


# --- data sources -------------------------------------------------------------


def create_data_source(
    organization_id: uuid.UUID,
    name: str,
    source_type: str,
    config: dict[str, Any],
    created_by: uuid.UUID | None,
) -> uuid.UUID:
    """Store a data source with its configuration encrypted.

    The redacted summary is computed here rather than at read time, so the column that sits *beside*
    the ciphertext cannot end up holding the thing the ciphertext protects. ``carries_secret`` is
    asserted against the summary for the same reason.
    """
    data_source_id = uuid.uuid4()
    summary = crypto.redact(config, source_type)
    leaked = crypto.carries_secret(summary)
    if leaked:
        # Unreachable through `redact`, which allowlists. Checked anyway: this is the one place a
        # mistake would put a password in a column the API returns.
        raise ValueError(f"refusing to store a summary containing {', '.join(leaked)}")

    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent.data_sources (data_source_id, organization_id, name, type, "
            "connection_config, summary, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                data_source_id,
                organization_id,
                name.strip(),
                source_type,
                crypto.encrypt_config(config),
                Jsonb(summary),
                created_by,
            ),
        )
    log.info(
        "data source created",
        data_source_id=str(data_source_id),
        organization_id=str(organization_id),
        type=source_type,
    )
    return data_source_id


def list_data_sources(organization_id: uuid.UUID) -> list[dict[str, Any]]:
    """Sources for one organisation. ``connection_config`` is not in the select list at all.

    Excluded rather than dropped afterwards: a column that never leaves the database cannot be
    forgotten about by a response model somebody writes later.
    """
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT data_source_id, organization_id, name, type, summary, created_at, "
            "       last_checked_at, last_status FROM agent.data_sources "
            "WHERE organization_id = %s ORDER BY created_at DESC",
            (organization_id,),
        )
        return list(cur.fetchall())


def get_data_source(
    organization_id: uuid.UUID, data_source_id: uuid.UUID, *, with_config: bool = False
) -> dict[str, Any] | None:
    """One source. ``with_config`` decrypts, and no API route passes it.

    The flag exists for the code that actually opens a connection. It is off by default so that
    reaching the secret is a deliberate act at the call site rather than a field somebody forgets
    to strip.
    """
    columns = (
        "data_source_id, organization_id, name, type, summary, created_at, last_checked_at, "
        "last_status"
    )
    if with_config:
        columns += ", connection_config"
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {columns} FROM agent.data_sources "  # noqa: S608 - fixed column list
            "WHERE organization_id = %s AND data_source_id = %s",
            (organization_id, data_source_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    if with_config:
        row["config"] = crypto.decrypt_config(row.pop("connection_config"))
    return row


def delete_data_source(organization_id: uuid.UUID, data_source_id: uuid.UUID) -> bool:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM agent.data_sources WHERE organization_id = %s AND data_source_id = %s",
            (organization_id, data_source_id),
        )
        return cur.rowcount == 1


def record_source_check(
    organization_id: uuid.UUID, data_source_id: uuid.UUID, status: str
) -> None:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.data_sources SET last_checked_at = now(), last_status = %s "
            "WHERE organization_id = %s AND data_source_id = %s",
            (status[:200], organization_id, data_source_id),
        )


# --- report sharing -----------------------------------------------------------


def set_report_visibility(
    organization_id: uuid.UUID, report_id: uuid.UUID, visibility: str
) -> bool:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.reports SET visibility = %s, updated_at = now() "
            "WHERE report_id = %s AND organization_id = %s",
            (visibility, report_id, organization_id),
        )
        return cur.rowcount == 1


def create_share(
    organization_id: uuid.UUID,
    report_id: uuid.UUID,
    audience: str,
    created_by: uuid.UUID | None,
    expires_in_hours: int | None,
) -> tuple[uuid.UUID, str, datetime | None] | None:
    """A link with a lifetime. Returns ``(share_id, token, expires_at)``.

    Returns None when the report is not this organisation's — the caller turns that into a 404
    rather than a 403, so a probe cannot confirm that somebody else's report exists.
    """
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM agent.reports WHERE report_id = %s AND organization_id = %s",
            (report_id, organization_id),
        )
        if cur.fetchone() is None:
            return None

        token, token_hash, prefix = crypto.new_token(crypto.SHARE_PREFIX)
        share_id = uuid.uuid4()
        expires_at = (
            datetime.now(UTC) + timedelta(hours=expires_in_hours) if expires_in_hours else None
        )
        cur.execute(
            "INSERT INTO agent.report_shares (share_id, report_id, token_hash, prefix, audience, "
            "created_by, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (share_id, report_id, token_hash, prefix, audience, created_by, expires_at),
        )
        cur.execute(
            "UPDATE agent.reports SET visibility = %s, updated_at = now() WHERE report_id = %s",
            (audience, report_id),
        )
    log.info(
        "report shared",
        report_id=str(report_id),
        audience=audience,
        expires_at=expires_at.isoformat() if expires_at else None,
    )
    return share_id, token, expires_at


def list_shares(organization_id: uuid.UUID, report_id: uuid.UUID) -> list[dict[str, Any]]:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT s.share_id, s.prefix, s.audience, s.created_at, s.expires_at, s.revoked_at, "
            "       s.last_used_at, s.use_count, u.email AS created_by_email "
            "FROM agent.report_shares s "
            "JOIN agent.reports r ON r.report_id = s.report_id "
            "LEFT JOIN agent.users u ON u.user_id = s.created_by "
            "WHERE s.report_id = %s AND r.organization_id = %s ORDER BY s.created_at DESC",
            (report_id, organization_id),
        )
        return list(cur.fetchall())


def revoke_share(organization_id: uuid.UUID, share_id: uuid.UUID) -> bool:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.report_shares s SET revoked_at = now() "
            "WHERE s.share_id = %s AND s.revoked_at IS NULL AND EXISTS ("
            "  SELECT 1 FROM agent.reports r WHERE r.report_id = s.report_id "
            "    AND r.organization_id = %s)",
            (share_id, organization_id),
        )
        return cur.rowcount == 1


def resolve_share(token: str) -> dict[str, Any] | None:
    """Turn a share token into a report.

    **The second function that looks across organisations**, and the only unauthenticated read path
    in the system. Three conditions, all in the SQL: the token hash matches, the share has not been
    revoked, and it has not expired. Checking expiry in Python instead would leave the decision to
    whichever caller remembered to make it.

    A team-audience share still requires the reader to be in the organisation; that check is the
    route's, because this function does not know who is asking. It returns the audience and the
    organisation so the route can make it.
    """
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT s.share_id, s.audience, s.expires_at, r.report_id, r.organization_id, "
            "       r.name, r.snapshot, r.created_at, r.updated_at "
            "FROM agent.report_shares s JOIN agent.reports r ON r.report_id = s.report_id "
            "WHERE s.token_hash = %s AND s.revoked_at IS NULL "
            "  AND (s.expires_at IS NULL OR s.expires_at > now())",
            (crypto.hash_token(token),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "UPDATE agent.report_shares SET last_used_at = now(), use_count = use_count + 1 "
            "WHERE share_id = %s",
            (row["share_id"],),
        )
    return row


# --- alerts -------------------------------------------------------------------


def create_alert(
    organization_id: uuid.UUID,
    name: str,
    metric: str,
    comparison: str,
    threshold: float,
    dimension: str | None,
    window_periods: int,
    created_by: uuid.UUID | None,
) -> uuid.UUID:
    alert_id = uuid.uuid4()
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent.alerts (alert_id, organization_id, name, metric, dimension, "
            "comparison, threshold, window_periods, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                alert_id,
                organization_id,
                name.strip(),
                metric,
                dimension,
                comparison,
                threshold,
                window_periods,
                created_by,
            ),
        )
    log.info("alert created", alert_id=str(alert_id), metric=metric, comparison=comparison)
    return alert_id


def list_alerts(organization_id: uuid.UUID) -> list[dict[str, Any]]:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT a.*, "
            "  (SELECT e.detail FROM agent.alert_events e WHERE e.alert_id = a.alert_id "
            "   ORDER BY e.created_at DESC LIMIT 1) AS last_detail, "
            "  (SELECT count(*) FROM agent.alert_events e "
            "   WHERE e.alert_id = a.alert_id AND e.triggered) AS times_triggered "
            "FROM agent.alerts a WHERE a.organization_id = %s ORDER BY a.created_at DESC",
            (organization_id,),
        )
        return list(cur.fetchall())


def get_alert(organization_id: uuid.UUID, alert_id: uuid.UUID) -> dict[str, Any] | None:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM agent.alerts WHERE alert_id = %s AND organization_id = %s",
            (alert_id, organization_id),
        )
        return cur.fetchone()


def set_alert_status(organization_id: uuid.UUID, alert_id: uuid.UUID, status: str) -> bool:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent.alerts SET status = %s WHERE alert_id = %s AND organization_id = %s",
            (status, alert_id, organization_id),
        )
        return cur.rowcount == 1


def delete_alert(organization_id: uuid.UUID, alert_id: uuid.UUID) -> bool:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM agent.alerts WHERE alert_id = %s AND organization_id = %s",
            (alert_id, organization_id),
        )
        return cur.rowcount == 1


def record_alert_event(
    alert_id: uuid.UUID,
    triggered: bool,
    detail: str,
    observed: float | None = None,
    baseline: float | None = None,
    change_pct: float | None = None,
    period: str | None = None,
    query_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Record an evaluation, fired or not.

    Both outcomes, because recording only the breaches would leave no way to tell a quiet alert
    from a broken one — and "we were never alerted" is the sentence that follows an alert that
    stopped running.
    """
    event_id = uuid.uuid4()
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent.alert_events (event_id, alert_id, triggered, observed, baseline, "
            "change_pct, period, detail, query_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                event_id,
                alert_id,
                triggered,
                observed,
                baseline,
                change_pct,
                period,
                detail,
                query_id,
            ),
        )
        cur.execute(
            "UPDATE agent.alerts SET last_checked_at = now(), "
            "last_triggered_at = CASE WHEN %s THEN now() ELSE last_triggered_at END, "
            "status = CASE WHEN %s THEN 'triggered' "
            "              WHEN status = 'triggered' THEN 'active' ELSE status END "
            "WHERE alert_id = %s AND status <> 'paused'",
            (triggered, triggered, alert_id),
        )
    return event_id


def alert_events(
    organization_id: uuid.UUID, alert_id: uuid.UUID, limit: int = 20
) -> list[dict[str, Any]]:
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT e.* FROM agent.alert_events e JOIN agent.alerts a ON a.alert_id = e.alert_id "
            "WHERE e.alert_id = %s AND a.organization_id = %s "
            "ORDER BY e.created_at DESC LIMIT %s",
            (alert_id, organization_id, limit),
        )
        return list(cur.fetchall())


def active_alerts_for_evaluation(organization_id: uuid.UUID) -> list[dict[str, Any]]:
    """Alerts worth checking: active or already triggered, never paused."""
    with rw_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM agent.alerts WHERE organization_id = %s AND status <> 'paused' "
            "ORDER BY created_at",
            (organization_id,),
        )
        return list(cur.fetchall())


# --- audit --------------------------------------------------------------------


def audit(
    principal: Principal,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    **detail: Any,
) -> None:
    """Append an entry. There is no update or delete path to this table anywhere.

    Never raises into the caller: an audit write failing must not turn a successful invitation into
    a 500. It is logged at error level instead, which is the signal an operator needs, and the
    action still happened.
    """
    try:
        with rw_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent.audit_log (organization_id, actor_user_id, actor_label, "
                "action, target_type, target_id, detail) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    principal.organization_id,
                    principal.user_id,
                    principal.label + (" (unauthenticated)" if principal.anonymous else ""),
                    action,
                    target_type,
                    target_id,
                    Jsonb(detail),
                ),
            )
    except Exception as exc:
        log.error("audit write failed", action=action, error=str(exc))


def audit_entries(
    organization_id: uuid.UUID, limit: int = 100, action: str | None = None
) -> list[dict[str, Any]]:
    with rw_conn() as conn, conn.cursor() as cur:
        if action:
            cur.execute(
                "SELECT * FROM agent.audit_log WHERE organization_id = %s AND action = %s "
                "ORDER BY created_at DESC, entry_id DESC LIMIT %s",
                (organization_id, action, limit),
            )
        else:
            cur.execute(
                "SELECT * FROM agent.audit_log WHERE organization_id = %s "
                "ORDER BY created_at DESC, entry_id DESC LIMIT %s",
                (organization_id, limit),
            )
        return list(cur.fetchall())
