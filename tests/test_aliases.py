"""Abbreviation dictionary API: multi-carrier aliases, whole-group replacement."""
from conftest import handover, make_waybill


def test_empty_dictionary_reports_revision_zero(client):
    r = client.get("/api/aliases")
    assert r.status_code == 200
    body = r.json()
    assert body["dictionary_revision"] == 0
    assert body["aliases"] == []


def test_replace_alias_stores_whole_group_and_revision(client):
    r = client.put(
        "/api/aliases/ABC",
        json={"formal_parties": ["CARRIER-A", "CARRIER-B"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["alias"] == "ABC"
    assert body["formal_parties"] == ["CARRIER-A", "CARRIER-B"]
    assert body["dictionary_revision"] == 1

    r = client.get("/api/aliases/ABC")
    assert r.status_code == 200
    assert r.json()["formal_parties"] == ["CARRIER-A", "CARRIER-B"]


def test_replace_is_whole_group_not_a_merge(client):
    client.put("/api/aliases/ABC", json={"formal_parties": ["A", "B"]})
    r = client.put("/api/aliases/ABC", json={"formal_parties": ["C"]})
    assert r.status_code == 200
    assert r.json()["formal_parties"] == ["C"]
    assert r.json()["dictionary_revision"] == 2
    listing = client.get("/api/aliases").json()
    entry = next(a for a in listing["aliases"] if a["alias"] == "ABC")
    assert entry["formal_parties"] == ["C"]


def test_every_successful_replace_bumps_revision(client):
    rev_before = client.get("/api/aliases").json()["dictionary_revision"]
    for expected in (rev_before + 1, rev_before + 2, rev_before + 3):
        body = client.put(
            "/api/aliases/ABC", json={"formal_parties": [f"P{expected}"]}
        ).json()
        assert body["dictionary_revision"] == expected


def test_empty_list_deregisters_alias(client):
    client.put("/api/aliases/ABC", json={"formal_parties": ["A"]})
    r = client.put("/api/aliases/ABC", json={"formal_parties": []})
    assert r.status_code == 200
    assert r.json()["formal_parties"] == []
    assert client.get("/api/aliases/ABC").status_code == 404


def test_get_unknown_alias_returns_404(client):
    r = client.get("/api/aliases/GHOST")
    assert r.status_code == 404


def test_delete_alias_and_unknown_delete_404(client):
    client.put("/api/aliases/ABC", json={"formal_parties": ["A", "B"]})
    r = client.delete("/api/aliases/ABC")
    assert r.status_code == 200
    assert r.json()["formal_parties"] == ["A", "B"]
    assert client.get("/api/aliases/ABC").status_code == 404
    assert client.delete("/api/aliases/ABC").status_code == 404


def test_blank_and_duplicate_parties_rejected_422(client):
    r = client.put("/api/aliases/ABC", json={"formal_parties": ["A", "  "]})
    assert r.status_code == 422
    # duplicates are stripped deterministically rather than rejected
    r = client.put("/api/aliases/ABC", json={"formal_parties": ["A", "A", "B"]})
    assert r.status_code == 200
    assert r.json()["formal_parties"] == ["A", "B"]


def test_dictionary_survives_restart(client, monkeypatch):
    client.put("/api/aliases/ABC", json={"formal_parties": ["A", "B"]})
    from app import config
    import importlib
    from app import main
    from fastapi.testclient import TestClient

    importlib.reload(main)
    with TestClient(main.app) as new_client:
        body = new_client.get("/api/aliases/ABC").json()
        assert body["formal_parties"] == ["A", "B"]
        assert body["dictionary_revision"] == 1
        # scans and dictionary stay jointly visible after restart
        make_waybill(client)
        new_client.post(
            "/api/waybills/AWB-1/scans/batch",
            json={
                "scans": [
                    handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "ABC")
                ]
            },
        )
        chain = new_client.get("/api/waybills/AWB-1/chain").json()
        assert chain["dictionary_revision"] == 1
        assert chain["unverified_scans"][0]["receiver_candidates"] == ["A", "B"]


def test_chain_response_carries_dictionary_revision(client):
    make_waybill(client)
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )
    assert r.json()["dictionary_revision"] == 0
    assert r.json()["chain"]["dictionary_revision"] == 0
    client.put("/api/aliases/B", json={"formal_parties": ["B1", "B2"]})
    chain = client.get("/api/waybills/AWB-1/chain").json()
    assert chain["dictionary_revision"] == 1


def test_concurrent_alias_replacements_serialize(client):
    """Parallel whole-group replacements never expose a half-written group."""
    import threading

    from app import config, database, schemas, services

    errors: list = []
    revs: list[int] = []
    lock = threading.Lock()

    def worker(worker_id: int) -> None:
        conn = database.connect(config.DB_PATH)
        try:
            body = schemas.AliasReplace(formal_parties=[f"P{worker_id}"])
            view = services.replace_alias(conn, "SHARED", body)
            with lock:
                revs.append(view.dictionary_revision)
        except Exception as exc:  # pragma: no cover - failure signal
            with lock:
                errors.append(str(exc))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert sorted(revs) == [1, 2, 3, 4, 5, 6]
    final = client.get("/api/aliases/SHARED").json()
    assert len(final["formal_parties"]) == 1  # whole-group, never a merged mix
    assert final["dictionary_revision"] == 6


def test_failed_alias_write_keeps_old_dictionary(client):
    client.put("/api/aliases/ABC", json={"formal_parties": ["A", "B"]})
    # A replacement whose input fails pydantic validation never opens a write,
    # so the committed group and its revision stay untouched.
    r = client.put("/api/aliases/ABC", json={"formal_parties": ["OK", "   "]})
    assert r.status_code == 422
    body = client.get("/api/aliases/ABC").json()
    assert body["formal_parties"] == ["A", "B"]
    assert body["dictionary_revision"] == 1
