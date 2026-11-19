"""Domain logic: revision arbitration, atomic batch apply, chain recomputation.

All write paths run inside one ``BEGIN IMMEDIATE`` transaction.  Any rule
violation raises ``DomainConflict`` / ``DomainValidation`` and rolls the whole
batch back, so the previously committed chain is preserved untouched.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .schemas import (
    AliasListResponse,
    AliasReplace,
    AliasView,
    BatchResponse,
    BatchSubmit,
    BreakReason,
    ChainResponse,
    ScanEntry,
    ScanView,
    WaybillCreate,
    entry_payload_hash,
    to_utc_iso,
    utcnow_iso,
)

_STORAGE_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"
_DICT_REV_KEY = "dictionary_revision"


class DomainConflict(Exception):
    """HTTP 409: same-revision divergence, stale revision, unknown scan ..."""


class DomainValidation(Exception):
    """HTTP 404: referenced waybill/resource does not exist."""


# --------------------------------------------------------------------------- #
# Waybills
# --------------------------------------------------------------------------- #
def create_waybill(conn: sqlite3.Connection, body: WaybillCreate) -> dict:
    from .database import transaction

    with transaction(conn):
        existing = conn.execute(
            "SELECT id FROM waybills WHERE id = ?", (body.waybill_id,)
        ).fetchone()
        if existing is not None:
            raise DomainConflict(f"waybill {body.waybill_id!r} already exists")
        conn.execute(
            "INSERT INTO waybills (id, origin_party, created_at) VALUES (?, ?, ?)",
            (body.waybill_id, body.origin_party, utcnow_iso()),
        )
    return {"waybill_id": body.waybill_id, "origin_party": body.origin_party}


def get_waybill(conn: sqlite3.Connection, waybill_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, origin_party FROM waybills WHERE id = ?", (waybill_id,)
    ).fetchone()
    if row is None:
        raise DomainValidation(f"waybill {waybill_id!r} not found")
    return row


# --------------------------------------------------------------------------- #
# Batch submission (handover reports, corrections, withdrawals)
# --------------------------------------------------------------------------- #
def apply_batch(
    conn: sqlite3.Connection, waybill_id: str, batch: BatchSubmit
) -> BatchResponse:
    from .database import transaction

    get_waybill(conn, waybill_id)  # 404-style check before opening the txn

    applied = 0
    idempotent = 0
    withdrawn: list[str] = []

    with transaction(conn):
        for entry in batch.scans:
            wrote = _apply_one(conn, waybill_id, entry)
            if not wrote:
                idempotent += 1
                continue
            if entry.event_type == "WITHDRAWAL":
                withdrawn.append(entry.scan_id)
            applied += 1

    # Recompute against the freshly committed snapshot.  Scan writes and the
    # returned query result therefore become visible together.
    chain = compute_chain(conn, waybill_id)
    return BatchResponse(
        waybill_id=waybill_id,
        applied=applied,
        idempotent_duplicates=idempotent,
        withdrawn_scan_ids=withdrawn,
        chain=chain,
        dictionary_revision=chain.dictionary_revision,
    )


def _apply_one(conn: sqlite3.Connection, waybill_id: str, entry: ScanEntry) -> bool:
    """Insert one scan revision.  Returns False when it was an idempotent no-op."""
    h = entry_payload_hash(entry)

    newest = conn.execute(
        "SELECT revision, event_type, payload_hash "
        "FROM scans WHERE waybill_id = ? AND scan_id = ? "
        "ORDER BY revision DESC LIMIT 1",
        (waybill_id, entry.scan_id),
    ).fetchone()

    if newest is not None and newest["revision"] > entry.revision:
        raise DomainConflict(
            f"scan_id={entry.scan_id!r} revision={entry.revision} is stale: "
            f"revision {newest['revision']} already adopted"
        )

    same_rev = conn.execute(
        "SELECT event_type, payload_hash FROM scans "
        "WHERE waybill_id = ? AND scan_id = ? AND revision = ?",
        (waybill_id, entry.scan_id, entry.revision),
    ).fetchone()

    if same_rev is not None:
        # 同修订异文 -> 整批拒绝；同文 -> 幂等跳过
        if same_rev["payload_hash"] != h:
            raise DomainConflict(
                f"scan_id={entry.scan_id!r} revision={entry.revision} already "
                "exists with different content (same-revision divergence)"
            )
        return False

    if entry.event_type == "WITHDRAWAL" and newest is None:
        raise DomainConflict(
            f"cannot withdraw unknown scan_id={entry.scan_id!r}: no prior revision exists"
        )

    ts_text = to_utc_iso(entry.ts_utc) if entry.ts_utc is not None else None
    conn.execute(
        "INSERT INTO scans (waybill_id, scan_id, revision, event_type, "
        "ts_utc, giver, receiver, payload_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            waybill_id,
            entry.scan_id,
            entry.revision,
            entry.event_type,
            ts_text,
            entry.giver,
            entry.receiver,
            h,
            utcnow_iso(),
        ),
    )
    return True


# --------------------------------------------------------------------------- #
# Abbreviation dictionary
# --------------------------------------------------------------------------- #
def _get_dict_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = ?", (_DICT_REV_KEY,)
    ).fetchone()
    return int(row["value"]) if row is not None else 0


def _bump_dict_revision(conn: sqlite3.Connection) -> int:
    rev = _get_dict_revision(conn) + 1
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_DICT_REV_KEY, str(rev)),
    )
    return rev


def _load_alias_map(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Committed alias -> formal custodians, in stored (insertion) order."""
    mapping: dict[str, list[str]] = {}
    rows = conn.execute(
        "SELECT alias, formal_party FROM aliases ORDER BY rowid ASC"
    ).fetchall()
    for row in rows:
        mapping.setdefault(row["alias"], []).append(row["formal_party"])
    return mapping


def list_aliases(conn: sqlite3.Connection) -> AliasListResponse:
    from .database import read_transaction

    with read_transaction(conn):
        revision = _get_dict_revision(conn)
        mapping = _load_alias_map(conn)
    return AliasListResponse(
        dictionary_revision=revision,
        aliases=[
            AliasView(alias=alias, formal_parties=parties, dictionary_revision=revision)
            for alias, parties in sorted(mapping.items())
        ],
    )


def get_alias(conn: sqlite3.Connection, alias: str) -> AliasView:
    from .database import read_transaction

    alias = alias.strip()
    with read_transaction(conn):
        revision = _get_dict_revision(conn)
        rows = conn.execute(
            "SELECT formal_party FROM aliases WHERE alias = ? ORDER BY rowid ASC",
            (alias,),
        ).fetchall()
    if not rows:
        raise DomainValidation(f"alias {alias!r} is not registered")
    return AliasView(
        alias=alias,
        formal_parties=[r["formal_party"] for r in rows],
        dictionary_revision=revision,
    )


def replace_alias(conn: sqlite3.Connection, alias: str, body: AliasReplace) -> AliasView:
    """Whole-group replacement of the formal custodians behind one abbreviation.

    The delete + insert + revision bump happen in one ``BEGIN IMMEDIATE``
    transaction, so a failure keeps the previously committed dictionary (and
    therefore the previous chain conclusions) untouched.
    """
    from .database import transaction

    alias = alias.strip()
    if not alias:
        raise DomainValidation("alias must not be empty")

    with transaction(conn):
        conn.execute("DELETE FROM aliases WHERE alias = ?", (alias,))
        conn.executemany(
            "INSERT INTO aliases (alias, formal_party) VALUES (?, ?)",
            [(alias, party) for party in body.formal_parties],
        )
        revision = _bump_dict_revision(conn)

    # Replacement succeeded: every chain is re-evaluated against the new
    # dictionary on its next query/batch receipt (and after any restart).
    return AliasView(
        alias=alias,
        formal_parties=list(body.formal_parties),
        dictionary_revision=revision,
    )


def delete_alias(conn: sqlite3.Connection, alias: str) -> AliasView:
    """Deregister an abbreviation (its name then resolves as a formal name)."""
    from .database import transaction

    alias = alias.strip()
    with transaction(conn):
        existing = conn.execute(
            "SELECT formal_party FROM aliases WHERE alias = ? ORDER BY rowid ASC",
            (alias,),
        ).fetchall()
        if not existing:
            raise DomainValidation(f"alias {alias!r} is not registered")
        parties = [r["formal_party"] for r in existing]
        conn.execute("DELETE FROM aliases WHERE alias = ?", (alias,))
        revision = _bump_dict_revision(conn)
    return AliasView(
        alias=alias, formal_parties=parties, dictionary_revision=revision
    )


# --------------------------------------------------------------------------- #
# Chain recomputation
# --------------------------------------------------------------------------- #
def _adopted_handovers(
    conn: sqlite3.Connection, waybill_id: str
) -> list[sqlite3.Row]:
    """Return handover rows for each scan's highest revision, time-ordered.

    Scans whose highest revision is a WITHDRAWAL tombstone do not appear.
    """
    return conn.execute(
        """
        SELECT s.scan_id, s.revision, s.event_type, s.ts_utc, s.giver, s.receiver
        FROM scans AS s
        JOIN (
            SELECT scan_id, MAX(revision) AS max_revision
            FROM scans
            WHERE waybill_id = ?
            GROUP BY scan_id
        ) AS m
          ON m.scan_id = s.scan_id AND m.max_revision = s.revision
        WHERE s.waybill_id = ? AND s.event_type = 'HANDOVER'
        ORDER BY s.ts_utc ASC, s.scan_id ASC
        """,
        (waybill_id, waybill_id),
    ).fetchall()


def _parse_ts(text: str) -> datetime:
    return datetime.strptime(text, _STORAGE_FMT).replace(tzinfo=timezone.utc)


def _resolve(name: str, alias_map: dict[str, list[str]]) -> list[str]:
    """Registered abbreviation -> its formal custodian group; otherwise the
    unregistered name is treated as a formal name and resolves to itself."""
    return list(alias_map.get(name, [name]))


def compute_chain(conn: sqlite3.Connection, waybill_id: str) -> ChainResponse:
    from .database import read_transaction

    waybill = get_waybill(conn, waybill_id)
    origin = waybill["origin_party"]

    # The dictionary and the scans must come from the same committed snapshot:
    # a single read transaction pins both for the whole evaluation.
    with read_transaction(conn):
        dict_revision = _get_dict_revision(conn)
        alias_map = _load_alias_map(conn)
        rows = _adopted_handovers(conn, waybill_id)

        # Group by timestamp: simultaneous handovers cannot be ordered by guessing.
        groups: list[tuple[str, list[sqlite3.Row]]] = []
        for row in rows:
            if groups and groups[-1][0] == row["ts_utc"]:
                groups[-1][1].append(row)
            else:
                groups.append((row["ts_utc"], [row]))

        def view(
            row: sqlite3.Row,
            ok: bool,
            reason: BreakReason | None,
            detail: str | None = None,
            confirmed_giver: str | None = None,
            confirmed_receiver: str | None = None,
            corr_id: str | None = None,
            corr_rev: int | None = None,
        ) -> ScanView:
            return ScanView(
                scan_id=row["scan_id"],
                revision=row["revision"],
                event_type=row["event_type"],
                ts_utc=_parse_ts(row["ts_utc"]),
                giver=row["giver"],
                receiver=row["receiver"],
                verified=ok,
                unverified_reason=reason,
                giver_candidates=_resolve(row["giver"], alias_map),
                receiver_candidates=_resolve(row["receiver"], alias_map),
                confirmed_giver=confirmed_giver,
                confirmed_receiver=confirmed_receiver,
                corroborating_scan_id=corr_id,
                corroborating_revision=corr_rev,
                unverified_detail=detail,
            )

        verified: list[ScanView] = []
        unverified: list[ScanView] = []
        custodian = origin  # 运单起始保管方以创建时的原登记为准，不做字典展开
        broken = False
        break_reason = BreakReason.none
        break_detail: str | None = None
        break_scan_ids: list[str] = []

        for idx, (ts, group) in enumerate(groups):
            if broken:
                # 断链之后的扫描一律未证实，直到补报/更正/撤回补齐缺口
                unverified.extend(
                    view(r, False, break_reason, break_detail) for r in group
                )
                continue

            if len(group) > 1:
                # 同刻多笔照旧断链，不猜测先后顺序
                broken = True
                break_reason = BreakReason.simultaneous
                break_scan_ids = [r["scan_id"] for r in group]
                break_detail = (
                    f"{len(group)} handovers share ts_utc={ts}; order cannot be "
                    "determined, so none of them is verified"
                )
                unverified.extend(
                    view(r, False, BreakReason.simultaneous, break_detail)
                    for r in group
                )
                continue

            scan = group[0]
            giver_candidates = _resolve(scan["giver"], alias_map)
            receiver_candidates = _resolve(scan["receiver"], alias_map)

            # 本笔的交出候选须包含上一保管方（首笔对照起始保管方）
            if custodian not in giver_candidates:
                broken = True
                break_reason = BreakReason.gap
                break_scan_ids = [scan["scan_id"]]
                break_detail = (
                    f"giver {scan['giver']!r} resolves to {giver_candidates}, "
                    f"which does not hold custody; expected {custodian!r} "
                    "(previous receiver / origin party) to be among the giver candidates"
                )
                unverified.append(
                    view(scan, False, BreakReason.gap, break_detail)
                )
                continue

            if len(receiver_candidates) == 1:
                # 唯一候选（含未登记正式名）直接确认，无需旁证
                confirmed_receiver = receiver_candidates[0]
                verified.append(
                    view(
                        scan,
                        True,
                        None,
                        confirmed_giver=custodian,
                        confirmed_receiver=confirmed_receiver,
                    )
                )
                custodian = confirmed_receiver
                continue

            # 接收候选有多个：只用紧接的下一时刻作旁证
            next_group = groups[idx + 1][1] if idx + 1 < len(groups) else None
            if next_group is None:
                broken = True
                break_reason = BreakReason.ambiguous
                break_scan_ids = [scan["scan_id"]]
                break_detail = (
                    f"receiver {scan['receiver']!r} resolves to multiple "
                    f"candidates {receiver_candidates}; no later handover exists "
                    "to act as corroboration"
                )
                unverified.append(
                    view(scan, False, BreakReason.ambiguous, break_detail)
                )
                continue

            if len(next_group) != 1:
                broken = True
                break_reason = BreakReason.ambiguous
                break_scan_ids = [scan["scan_id"]]
                break_detail = (
                    f"receiver {scan['receiver']!r} resolves to multiple "
                    f"candidates {receiver_candidates}; the immediately following "
                    f"timestamp has {len(next_group)} handovers, so it cannot serve "
                    "as a unique corroborating scan"
                )
                unverified.append(
                    view(scan, False, BreakReason.ambiguous, break_detail)
                )
                continue

            # 该时刻须恰有一笔有效扫描，且它的交出候选与本笔接收候选只有一家重合
            witness = next_group[0]
            witness_giver_candidates = _resolve(witness["giver"], alias_map)
            overlap = [
                party
                for party in receiver_candidates
                if party in witness_giver_candidates
            ]
            if len(overlap) != 1:
                broken = True
                break_reason = BreakReason.ambiguous
                break_scan_ids = [scan["scan_id"]]
                break_detail = (
                    f"receiver {scan['receiver']!r} resolves to candidates "
                    f"{receiver_candidates}; the next scan {witness['scan_id']!r} "
                    f"giver candidates {witness_giver_candidates} overlap in "
                    f"{overlap} (exactly one overlap is required to confirm the "
                    "receiver), so the chain breaks here"
                )
                unverified.append(
                    view(scan, False, BreakReason.ambiguous, break_detail)
                )
                continue

            confirmed_receiver = overlap[0]
            verified.append(
                view(
                    scan,
                    True,
                    None,
                    confirmed_giver=custodian,
                    confirmed_receiver=confirmed_receiver,
                    corr_id=witness["scan_id"],
                    corr_rev=witness["revision"],
                )
            )
            custodian = confirmed_receiver

    return ChainResponse(
        waybill_id=waybill_id,
        origin_party=origin,
        current_custodian=custodian,
        verified_prefix=verified,
        unverified_scans=unverified,
        chain_intact=not broken,
        break_reason=break_reason,
        break_detail=break_detail,
        break_scan_ids=break_scan_ids,
        dictionary_revision=dict_revision,
    )


# Re-export so other modules can reuse the domain exceptions.
__all__ = [
    "DomainConflict",
    "DomainValidation",
    "apply_batch",
    "compute_chain",
    "create_waybill",
    "delete_alias",
    "get_alias",
    "get_waybill",
    "list_aliases",
    "replace_alias",
]
