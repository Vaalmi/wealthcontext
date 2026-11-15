"""Creation-time bindings for chat ownership and original message associations.

These hashes detect inconsistent metadata and authentic-record transplantation;
they are not signatures against an administrator replacing every evidence row.
"""

import sqlite3

from wealthcontext.storage import canonical, digest


def _verify(proof: sqlite3.Row | None, expected: dict) -> None:
    if (proof is None or proof["schema_version"] != 1
            or proof["payload"] != canonical(expected)
            or digest(proof["payload"]) != proof["payload_hash"]):
        raise sqlite3.DatabaseError("Conversation creation evidence unavailable")


def record_conversation(
    connection: sqlite3.Connection, conversation_id: str, principal_id: str, created_at: str,
) -> None:
    payload = canonical({"conversation_id": conversation_id, "principal_id": principal_id,
                         "created_at": created_at})
    connection.execute("INSERT INTO conversation_creation_integrity VALUES (?,1,?,?)",
                       (conversation_id, payload, digest(payload)))


def conversation_owner(connection: sqlite3.Connection, conversation_id: str) -> str | None:
    row = connection.execute(
        "SELECT * FROM conversations WHERE id=?", (conversation_id,),
    ).fetchone()
    if row is None:
        return None
    proof = connection.execute("SELECT * FROM conversation_creation_integrity "
                               "WHERE conversation_id=?", (conversation_id,)).fetchone()
    _verify(proof, {"conversation_id": conversation_id, "principal_id": row["principal_id"],
                    "created_at": row["created_at"]})
    return row["principal_id"]


def record_message(
    connection: sqlite3.Connection, conversation_id: str, principal_id: str, sequence: int,
    kind: str, payload_hash: str, recorded_at: str, result_ids: list[str],
) -> None:
    payload = canonical({"conversation_id": conversation_id, "principal_id": principal_id,
                         "sequence": sequence, "kind": kind, "payload_hash": payload_hash,
                         "recorded_at": recorded_at, "result_ids": sorted(set(result_ids))})
    connection.execute("INSERT INTO conversation_message_integrity VALUES (?,?,1,?,?)",
                       (conversation_id, sequence, payload, digest(payload)))


def verify_messages(
    connection: sqlite3.Connection, conversation_id: str, principal_id: str,
) -> None:
    rows = connection.execute("SELECT * FROM conversation_messages WHERE conversation_id=? "
                              "ORDER BY sequence", (conversation_id,)).fetchall()
    proofs = {row["sequence"]: row for row in connection.execute(
        "SELECT * FROM conversation_message_integrity WHERE conversation_id=?", (conversation_id,),
    )}
    if set(proofs) != {row["sequence"] for row in rows}:
        raise sqlite3.DatabaseError("Conversation message inventory mismatch")
    for row in rows:
        refs = [item[0] for item in connection.execute(
            "SELECT result_id FROM conversation_message_results WHERE conversation_id=? "
            "AND sequence=? ORDER BY result_id", (conversation_id, row["sequence"]),
        )]
        _verify(proofs.get(row["sequence"]), {
            "conversation_id": conversation_id, "principal_id": principal_id,
            "sequence": row["sequence"], "kind": row["kind"], "payload_hash": row["payload_hash"],
            "recorded_at": row["recorded_at"], "result_ids": refs,
        })
        if digest(row["payload"]) != row["payload_hash"]:
            raise sqlite3.DatabaseError("Conversation message payload mismatch")
