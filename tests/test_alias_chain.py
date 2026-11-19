"""Alias-aware chain evaluation: candidate resolution and next-timestamp corroboration."""
from conftest import handover, make_waybill, withdrawal


def _put_alias(client, alias, parties):
    r = client.put(f"/api/aliases/{alias}", json={"formal_parties": parties})
    assert r.status_code == 200, r.text


def test_unregistered_name_treated_as_formal_name(client):
    make_waybill(client, origin="ORIG")
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "SOLO")]},
    )
    chain = r.json()["chain"]
    scan = chain["verified_prefix"][0]
    assert scan["receiver_candidates"] == ["SOLO"]
    assert scan["confirmed_receiver"] == "SOLO"
    assert scan["corroborating_scan_id"] is None
    assert chain["current_custodian"] == "SOLO"


def test_origin_party_kept_as_originally_registered(client):
    # ORIG is registered as an alias but the waybill's origin stays literal:
    # the custodian is "ORIG", and an aliased giver group must contain that
    # literal value to be accepted — dictionary expansion never rewrites it.
    _put_alias(client, "ORIG", ["FORMAL-ORIG-1", "FORMAL-ORIG-2"])
    make_waybill(client, origin="ORIG")
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "FORMAL-ORIG-1", "B")]},
    )
    chain = r.json()["chain"]
    assert chain["break_reason"] == "GAP"
    assert chain["current_custodian"] == "ORIG"

    # Even reusing the registered name expands to the formal group, which no
    # longer contains the literal origin -> still a GAP (origin never expands).
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S2", 1, "2026-11-18T09:00:00Z", "ORIG", "B")]},
    )
    chain = r.json()["chain"]
    assert chain["break_reason"] == "GAP"
    s2 = next(s for s in chain["unverified_scans"] if s["scan_id"] == "S2")
    assert s2["giver_candidates"] == [
        "FORMAL-ORIG-1",
        "FORMAL-ORIG-2",
    ]

    # Replace the group so it includes the literal origin: on a fresh waybill
    # the handover verifies, with the confirmed giver equal to the origin.
    client.put(
        "/api/aliases/ORIG",
        json={"formal_parties": ["ORIG", "FORMAL-ORIG-1"]},
    )
    make_waybill(client, wid="AWB-2", origin="ORIG")
    r = client.post(
        "/api/waybills/AWB-2/scans/batch",
        json={"scans": [handover("S3", 1, "2026-11-18T10:00:00Z", "ORIG", "B")]},
    )
    chain = r.json()["chain"]
    assert chain["chain_intact"] is True
    assert chain["verified_prefix"][0]["confirmed_giver"] == "ORIG"


def test_giver_alias_must_include_current_custodian(client):
    make_waybill(client, origin="ORIG")
    _put_alias(client, "GW", ["X", "Y"])
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "GW", "B")]},
    )
    chain = r.json()["chain"]
    assert chain["break_reason"] == "GAP"
    s1 = chain["unverified_scans"][0]
    assert s1["giver_candidates"] == ["X", "Y"]
    assert "ORIG" in s1["unverified_detail"]


def test_multi_receiver_confirmed_by_unique_next_scan_overlap(client):
    make_waybill(client, origin="ORIG")
    _put_alias(client, "CARRIERS", ["CA", "CB"])
    # S1 hands to an ambiguous abbreviation; S2 at the next timestamp gives
    # from exactly one of the candidates -> receiver uniquely confirmed.
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "CARRIERS"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "CA", "D"),
            ]
        },
    )
    chain = r.json()["chain"]
    assert chain["chain_intact"] is True
    s1 = chain["verified_prefix"][0]
    assert s1["confirmed_receiver"] == "CA"
    assert s1["confirmed_giver"] == "ORIG"
    assert s1["corroborating_scan_id"] == "S2"
    assert s1["corroborating_revision"] == 1
    assert chain["current_custodian"] == "D"


def test_ambiguous_when_no_later_scan_exists(client):
    make_waybill(client, origin="ORIG")
    _put_alias(client, "CARRIERS", ["CA", "CB"])
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "CARRIERS")]},
    )
    chain = r.json()["chain"]
    assert chain["break_reason"] == "AMBIGUOUS"
    assert chain["break_scan_ids"] == ["S1"]
    s1 = chain["unverified_scans"][0]
    assert s1["receiver_candidates"] == ["CA", "CB"]
    assert s1["confirmed_receiver"] is None
    assert "no later handover" in s1["unverified_detail"]
    # custodian does not advance past an unconfirmed handover
    assert chain["current_custodian"] == "ORIG"


def test_ambiguous_when_overlap_is_not_unique(client):
    make_waybill(client, origin="ORIG")
    _put_alias(client, "CARRIERS", ["CA", "CB"])
    _put_alias(client, "NEXT-GIVER", ["CA", "CB"])  # overlaps both candidates
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "CARRIERS"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "NEXT-GIVER", "D"),
            ]
        },
    )
    chain = r.json()["chain"]
    assert chain["break_reason"] == "AMBIGUOUS"
    detail = chain["unverified_scans"][0]["unverified_detail"]
    assert "exactly one overlap" in detail


def test_ambiguous_when_no_overlap_at_all(client):
    make_waybill(client, origin="ORIG")
    _put_alias(client, "CARRIERS", ["CA", "CB"])
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "CARRIERS"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "CC", "D"),
            ]
        },
    )
    chain = r.json()["chain"]
    assert chain["break_reason"] == "AMBIGUOUS"
    assert chain["break_scan_ids"] == ["S1"]
    # and everything after stays unverified
    assert [s["scan_id"] for s in chain["unverified_scans"]] == ["S1", "S2"]


def test_next_timestamp_with_multiple_scans_cannot_corroborate(client):
    make_waybill(client, origin="ORIG")
    _put_alias(client, "CARRIERS", ["CA", "CB"])
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "CARRIERS"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "CA", "D"),
                handover("S3", 1, "2026-11-18T09:00:00Z", "CB", "E"),
            ]
        },
    )
    chain = r.json()["chain"]
    assert chain["break_reason"] == "AMBIGUOUS"
    assert chain["break_scan_ids"] == ["S1"]
    detail = chain["unverified_scans"][0]["unverified_detail"]
    assert "2 handovers" in detail


def test_only_immediately_following_timestamp_is_used(client):
    make_waybill(client, origin="ORIG")
    _put_alias(client, "CARRIERS", ["CA", "CB"])
    # S2 (next timestamp) cannot disambiguate; S3 one slot later would, but
    # must not be consulted — the chain breaks at S1.
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "CARRIERS"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "ZZ", "D"),
                handover("S3", 1, "2026-11-18T10:00:00Z", "CA", "E"),
            ]
        },
    )
    chain = r.json()["chain"]
    assert chain["break_reason"] == "AMBIGUOUS"
    assert chain["break_scan_ids"] == ["S1"]


def test_simultaneous_group_still_breaks_even_with_alias(client):
    make_waybill(client, origin="ORIG")
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
                handover("S3", 1, "2026-11-18T09:00:00Z", "C", "D"),
            ]
        },
    )
    chain = r.json()["chain"]
    assert chain["break_reason"] == "SIMULTANEOUS"
    assert sorted(chain["break_scan_ids"]) == ["S2", "S3"]


def test_witness_correction_rejudges_previous_scan(client):
    make_waybill(client, origin="ORIG")
    _put_alias(client, "CARRIERS", ["CA", "CB"])
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "CARRIERS"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "CA", "D"),
            ]
        },
    )
    chain = client.get("/api/waybills/AWB-1/chain").json()
    assert chain["verified_prefix"][0]["confirmed_receiver"] == "CA"

    # Correct the witness: its giver no longer overlaps any candidate -> S1 re-judged.
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S2", 2, "2026-11-18T09:00:00Z", "ZZ", "D")]},
    )
    chain = r.json()["chain"]
    assert chain["break_reason"] == "AMBIGUOUS"
    assert chain["break_scan_ids"] == ["S1"]
    assert chain["current_custodian"] == "ORIG"


def test_witness_withdrawal_rejudges_previous_scan(client):
    make_waybill(client, origin="ORIG")
    _put_alias(client, "CARRIERS", ["CA", "CB"])
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "CARRIERS"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "CA", "D"),
            ]
        },
    )
    # Withdraw the only witness -> S1 loses its corroboration and breaks.
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [withdrawal("S2", 2)]},
    )
    chain = r.json()["chain"]
    assert chain["break_reason"] == "AMBIGUOUS"
    assert chain["break_scan_ids"] == ["S1"]
    assert "no later handover" in chain["break_detail"]


def test_backfilling_witness_heals_ambiguous_chain(client):
    make_waybill(client, origin="ORIG")
    _put_alias(client, "CARRIERS", ["CA", "CB"])
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "CARRIERS")]},
    )
    assert client.get("/api/waybills/AWB-1/chain").json()["break_reason"] == "AMBIGUOUS"

    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S2", 1, "2026-11-18T09:00:00Z", "CB", "D")]},
    )
    chain = r.json()["chain"]
    assert chain["chain_intact"] is True
    s1 = chain["verified_prefix"][0]
    assert s1["confirmed_receiver"] == "CB"
    assert s1["corroborating_scan_id"] == "S2"
    assert s1["corroborating_revision"] == 1


def test_dictionary_replacement_rejudges_chains(client):
    make_waybill(client, origin="ORIG")
    # Without a dictionary both scans verify with literal names.
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
            ]
        },
    )
    assert client.get("/api/waybills/AWB-1/chain").json()["chain_intact"] is True

    # Register B as an ambiguous abbreviation: S1 now needs corroboration,
    # and S2's literal giver 'B' overlaps both candidates -> ambiguous.
    client.put("/api/aliases/B", json={"formal_parties": ["B1", "B2"]})
    chain = client.get("/api/waybills/AWB-1/chain").json()
    assert chain["break_reason"] == "AMBIGUOUS"
    assert chain["dictionary_revision"] == 1

    # Replace the group so it contains a single formal party: chain heals.
    client.put("/api/aliases/B", json={"formal_parties": ["B1"]})
    chain = client.get("/api/waybills/AWB-1/chain").json()
    assert chain["chain_intact"] is True
    assert chain["verified_prefix"][0]["confirmed_receiver"] == "B1"
    assert chain["current_custodian"] == "C"  # C unregistered -> formal name

    # Deregister the alias: literal semantics return.
    client.delete("/api/aliases/B")
    chain = client.get("/api/waybills/AWB-1/chain").json()
    assert chain["chain_intact"] is True
    assert chain["verified_prefix"][0]["confirmed_receiver"] == "B"


def test_corroboration_uses_adopted_revision(client):
    make_waybill(client, origin="ORIG")
    _put_alias(client, "CARRIERS", ["CA", "CB"])
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "CARRIERS"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "CA", "D"),
            ]
        },
    )
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S2", 3, "2026-11-18T09:00:00Z", "CA", "D2")]},
    )
    chain = client.get("/api/waybills/AWB-1/chain").json()
    s1 = chain["verified_prefix"][0]
    assert s1["corroborating_scan_id"] == "S2"
    assert s1["corroborating_revision"] == 3
