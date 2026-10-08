"""Reads and writes of agent_audit_log. Runs inside the caller's transaction and never commits."""

import json
import uuid
from typing import Any

from sqlalchemy import Connection, text


def insert_audit_record(
    conn: Connection,
    *,
    operation_id: str,
    user_id: uuid.UUID,
    role: str,
    operation: str,
    entity_type: str,
    entity_id: str | None,
    before_state: dict[str, Any] | None,
    after_state: dict[str, Any] | None,
    status: str,
    error: str | None = None,
    thread_id: str | None = None,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO agent_audit_log
                (operation_id, user_id, role, operation, entity_type, entity_id,
                 before_state, after_state, status, error, thread_id, completed_at)
            VALUES (:operation_id, :user_id, :role, :operation, :entity_type, :entity_id,
                    CAST(:before_state AS jsonb), CAST(:after_state AS jsonb),
                    :status, :error, :thread_id, now())
            """
        ),
        {
            "operation_id": operation_id,
            "user_id": user_id,
            "role": role,
            "operation": operation,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "before_state": _to_json(before_state),
            "after_state": _to_json(after_state),
            "status": status,
            "error": error,
            "thread_id": thread_id,
        },
    )


# --- mutation requests (Layer 8) ---------------------------------------------
# A request row tracks one proposed agent mutation: pending until the user confirms,
# then success or failed. Its operation_id is the idempotency key. Layer 4 still
# writes its own success row for the executed operation.


def lock_thread(conn: Connection, thread_id: str) -> None:
    """Serialise proposals within one conversation until the transaction ends."""
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:thread_id, 0))"), {"thread_id": thread_id})


def insert_request(
    conn: Connection,
    *,
    operation_id: str,
    user_id: uuid.UUID,
    role: str,
    operation: str,
    entity_type: str,
    before_state: dict[str, Any],
    thread_id: str,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO agent_audit_log
                (operation_id, user_id, role, operation, entity_type, before_state, status, thread_id)
            VALUES (:operation_id, :user_id, :role, :operation, :entity_type, CAST(:before_state AS jsonb),
                    'pending', :thread_id)
            """
        ),
        {
            "operation_id": operation_id,
            "user_id": user_id,
            "role": role,
            "operation": operation,
            "entity_type": entity_type,
            "before_state": _to_json(before_state),
            "thread_id": thread_id,
        },
    )


def find_pending_request(
    conn: Connection,
    *,
    user_id: uuid.UUID,
    thread_id: str,
    operation: str,
    arguments: dict[str, Any],
    ttl_seconds: int,
) -> str | None:
    """The operation_id of an unexpired pending request with exactly these arguments, if any."""
    return conn.execute(
        text(
            """
            SELECT operation_id FROM agent_audit_log
            WHERE user_id = :user_id AND thread_id = :thread_id AND operation = :operation AND status = 'pending'
              AND before_state -> 'arguments' = CAST(:arguments AS jsonb)
              AND now() - created_at <= make_interval(secs => :ttl)
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"user_id": user_id, "thread_id": thread_id, "operation": operation, "arguments": _to_json(arguments),
         "ttl": ttl_seconds},
    ).scalar()


def close_pending_requests(
    conn: Connection, *, user_id: uuid.UUID, thread_id: str, operation_suffix: str, after_state: dict[str, Any],
    error: str,
) -> int:
    """Mark every pending request of this user's conversation as failed (e.g. superseded)."""
    return conn.execute(
        text(
            """
            UPDATE agent_audit_log
            SET status = 'failed', after_state = CAST(:after_state AS jsonb), error = :error, completed_at = now()
            WHERE user_id = :user_id AND thread_id = :thread_id AND status = 'pending'
              AND operation LIKE '%' || :suffix
            """
        ),
        {"user_id": user_id, "thread_id": thread_id, "suffix": operation_suffix, "after_state": _to_json(after_state),
         "error": error},
    ).rowcount


def lock_request(
    conn: Connection, *, operation_id: str, user_id: uuid.UUID, thread_id: str, ttl_seconds: int
) -> dict[str, Any] | None:
    """Row-lock one request of this user's conversation until the transaction ends."""
    row = conn.execute(
        text(
            """
            SELECT operation_id, operation, status, before_state, after_state, error,
                   now() - created_at > make_interval(secs => :ttl) AS expired
            FROM agent_audit_log
            WHERE operation_id = :operation_id AND user_id = :user_id AND thread_id = :thread_id
            FOR UPDATE
            """
        ),
        {"operation_id": operation_id, "user_id": user_id, "thread_id": thread_id, "ttl": ttl_seconds},
    ).mappings().first()
    return dict(row) if row else None


def finish_request(
    conn: Connection,
    *,
    operation_id: str,
    status: str,
    after_state: dict[str, Any],
    entity_id: str | None = None,
    error: str | None = None,
) -> int:
    """Move a pending request to success or failed. Returns 0 if it was no longer pending."""
    return conn.execute(
        text(
            """
            UPDATE agent_audit_log
            SET status = :status, entity_id = :entity_id, after_state = CAST(:after_state AS jsonb),
                error = :error, completed_at = now()
            WHERE operation_id = :operation_id AND status = 'pending'
            """
        ),
        {"operation_id": operation_id, "status": status, "entity_id": entity_id,
         "after_state": _to_json(after_state), "error": error},
    ).rowcount


def note_duplicate(conn: Connection, operation_id: str) -> None:
    """Count a repeated confirmation of an already executed request."""
    conn.execute(
        text(
            """
            UPDATE agent_audit_log
            SET after_state = COALESCE(after_state, '{}'::jsonb) || jsonb_build_object(
                'duplicate_confirmations', COALESCE((after_state ->> 'duplicate_confirmations')::int, 0) + 1)
            WHERE operation_id = :operation_id
            """
        ),
        {"operation_id": operation_id},
    )


def _to_json(state: dict[str, Any] | None) -> str | None:
    # default=str keeps Decimal precision ("102.50") and renders datetimes/UUIDs readably.
    return None if state is None else json.dumps(state, default=str)
