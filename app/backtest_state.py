from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

from .backtest_metadata import (
    BACKTEST_DIRTY_META_KEY,
    BACKTEST_REBUILD_LAST_ATTEMPT_META_KEY,
    BACKTEST_REBUILD_REQUIRED_META_KEY,
    BACKTEST_REFRESH_LEASE_META_KEY,
    LEGACY_BACKTEST_DIRTY_META_KEY,
)
from .db import get_meta, set_meta


_ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def load_backtest_dirty(con: sqlite3.Connection) -> dict[str, str]:
    """Return the earliest dirty anchor per fund across new and legacy state."""
    merged: dict[str, str] = {}
    for key in (LEGACY_BACKTEST_DIRTY_META_KEY, BACKTEST_DIRTY_META_KEY):
        raw = get_meta(con, key, {})
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"metadata {key!r} must contain an object")
        for code, from_date in raw.items():
            if not isinstance(code, str):
                raise ValueError(f"metadata {key!r} contains a non-string fund code")
            anchor = _validate_iso_date(from_date, field=f"{key}[{code!r}]")
            if code not in merged or anchor < merged[code]:
                merged[code] = anchor
    return merged


def mark_backtest_dirty(
    con: sqlite3.Connection,
    code: str,
    from_date: str,
) -> str:
    """Create or move a fund's dirty anchor earlier, without committing."""
    anchor = _validate_iso_date(from_date, field="from_date")
    dirty = load_backtest_dirty(con)
    final_anchor = min(anchor, dirty[code]) if code in dirty else anchor
    dirty[code] = final_anchor
    _store_backtest_dirty(con, dirty)
    return final_anchor


def invalidate_backtests(
    con: sqlite3.Connection,
    code: str,
    from_date: str,
) -> str:
    """Mark a fund dirty and remove affected derived rows in the transaction."""
    final_anchor = mark_backtest_dirty(con, code, from_date)
    con.execute(
        "delete from backtests where fund_code = ? and date >= ?",
        (code, final_anchor),
    )
    return final_anchor


def resolve_backtest_dirty(
    con: sqlite3.Connection,
    code: str,
    *,
    full_replace: bool,
    processed_from: str | None,
    processed_through: str | None,
) -> bool:
    """Clear a dirty anchor only when the replacement covered its date."""
    normalized_from = (
        _validate_iso_date(processed_from, field="processed_from")
        if processed_from is not None
        else None
    )
    normalized_through = (
        _validate_iso_date(processed_through, field="processed_through")
        if processed_through is not None
        else None
    )

    dirty = load_backtest_dirty(con)
    anchor = dirty.get(code)
    if anchor is None:
        return False

    if full_replace:
        covered = normalized_through is not None and normalized_through >= anchor
    else:
        covered = (
            normalized_from is not None
            and normalized_through is not None
            and normalized_from <= anchor <= normalized_through
        )
    if not covered:
        return False

    del dirty[code]
    _store_backtest_dirty(con, dirty)
    return True


def clear_backtest_rebuild_required_if_resolved(
    con: sqlite3.Connection,
    configured_codes: set[str],
) -> bool:
    """Clear the upgrade marker after every configured dirty fund was covered."""
    if any(code in configured_codes for code in load_backtest_dirty(con)):
        return False
    cursor = con.execute(
        "delete from metadata where key in (?, ?)",
        (
            BACKTEST_REBUILD_REQUIRED_META_KEY,
            BACKTEST_REBUILD_LAST_ATTEMPT_META_KEY,
        ),
    )
    return cursor.rowcount > 0


def claim_backtest_refresh_lease(
    con: sqlite3.Connection,
    owner: str,
    *,
    require_rebuild: bool,
    now_at: str,
    lease_seconds: int,
    retry_interval_seconds: int,
) -> bool:
    """Claim the shared SQLite lease inside a caller-owned write transaction."""
    now = _parse_datetime(now_at, field="now_at")
    rebuild_required = bool(get_meta(con, BACKTEST_REBUILD_REQUIRED_META_KEY))
    if require_rebuild and (
        not rebuild_required or bool(get_meta(con, "backtests_disabled", False))
    ):
        return False
    if require_rebuild:
        last_attempt_at = get_meta(con, BACKTEST_REBUILD_LAST_ATTEMPT_META_KEY)
        if last_attempt_at:
            try:
                last_attempt = _parse_datetime(
                    last_attempt_at,
                    field=BACKTEST_REBUILD_LAST_ATTEMPT_META_KEY,
                )
            except ValueError:
                pass
            else:
                if (now - last_attempt).total_seconds() < retry_interval_seconds:
                    return False

    lease = get_meta(con, BACKTEST_REFRESH_LEASE_META_KEY)
    if isinstance(lease, dict) and lease.get("owner") != owner:
        expires_at = lease.get("expires_at")
        if isinstance(expires_at, str):
            try:
                if _parse_datetime(expires_at, field="lease.expires_at") > now:
                    return False
            except ValueError:
                pass

    set_meta(
        con,
        BACKTEST_REFRESH_LEASE_META_KEY,
        {
            "owner": owner,
            "claimed_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=lease_seconds)).isoformat(),
        },
    )
    if rebuild_required:
        set_meta(con, BACKTEST_REBUILD_LAST_ATTEMPT_META_KEY, now.isoformat())
    return True


def release_backtest_refresh_lease(
    con: sqlite3.Connection,
    owner: str,
) -> bool:
    lease = get_meta(con, BACKTEST_REFRESH_LEASE_META_KEY)
    if not isinstance(lease, dict) or lease.get("owner") != owner:
        return False
    con.execute(
        "delete from metadata where key = ?",
        (BACKTEST_REFRESH_LEASE_META_KEY,),
    )
    return True


def backtest_refresh_lease_active(
    con: sqlite3.Connection,
    *,
    now_at: str,
) -> bool:
    lease = get_meta(con, BACKTEST_REFRESH_LEASE_META_KEY)
    if not isinstance(lease, dict) or not isinstance(lease.get("expires_at"), str):
        return False
    try:
        return _parse_datetime(lease["expires_at"], field="lease.expires_at") > _parse_datetime(
            now_at,
            field="now_at",
        )
    except ValueError:
        return False


def _store_backtest_dirty(
    con: sqlite3.Connection,
    dirty: dict[str, str],
) -> None:
    set_meta(con, BACKTEST_DIRTY_META_KEY, dirty)
    con.execute(
        "delete from metadata where key = ?",
        (LEGACY_BACKTEST_DIRTY_META_KEY,),
    )


def _validate_iso_date(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _ISO_DATE_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be an ISO date in YYYY-MM-DD format")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid ISO date in YYYY-MM-DD format") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an ISO date in YYYY-MM-DD format")
    return value


def _parse_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
