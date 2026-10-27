"""Offline recovery oracles and actual first-read query profiles; no termination."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

from sec_ingestion_synthetic import make_payloads

from equitytrace.models import ResolvedTicker
from equitytrace.repositories import ingestion

TARGET = "0000999999"
OTHER = "0000888888"
TABLE_KEYS = {
    "issuers": "cik",
    "securities": "ticker, cik",
    "filings": "accession_number",
    "financial_facts": "fact_id",
    "ingestion_runs": "run_id",
}


class RecoveryMismatch(RuntimeError):
    """An observation cannot certify one whole publication."""


def write_json(path: Path, value: object) -> None:
    """Publish complete external evidence before a synchronization marker."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def canonical(value):
    if isinstance(value, datetime):
        return {"utc": value.astimezone(UTC).isoformat()}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, float):
        return {"double_hex": value.hex()}
    return value


def digest_query(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    columns = [item[0] for item in cursor.description]
    digest = hashlib.sha256(json.dumps(columns).encode())
    count = 0
    while rows := cursor.fetchmany(500):
        for row in rows:
            digest.update((json.dumps([canonical(v) for v in row], sort_keys=True) + "\n").encode())
            count += 1
    return {"columns": columns, "rows": count, "sha256": digest.hexdigest()}


def payloads(count, version, cik=TARGET):
    submissions, facts = make_payloads(count, float(version))
    # Distinct metadata, securities, filing availability and fact values for each
    # publication; retain accessions/observation coverage for legitimate replacement.
    submissions["cik"] = cik
    submissions["name"] = f"Synthetic {cik} publication {version}"
    ticker = "SYNTH" if cik == TARGET else "OTHER"
    submissions["tickers"] = [ticker, f"{ticker}.A" if version == 1 else f"{ticker}.B"]
    submissions["exchanges"] = ["Nasdaq", "Nasdaq"]
    recent = submissions["filings"]["recent"]
    recent["accessionNumber"] = [a.replace(TARGET, cik) for a in recent["accessionNumber"]]
    recent["acceptanceDateTime"] = [
        f"2026-01-{2 * version + i % 2:02d}T21:00:00Z"
        for i in range(len(recent["accessionNumber"]))
    ]
    recent["primaryDocument"] = [
        f"publication-{version}-{i}.htm" for i in range(len(recent["accessionNumber"]))
    ]
    facts["cik"] = int(cik)
    facts["entityName"] = submissions["name"]
    for concept in facts["facts"]["us-gaap"].values():
        for observation in concept["units"]["USD"]:
            observation["accn"] = observation["accn"].replace(TARGET, cik)
    return submissions, facts


def publish(db, count, version, *, cik=TARGET, boundary=None, started=None):
    evidence = payloads(count, version, cik)
    ticker = "SYNTH" if cik == TARGET else "OTHER"
    original_start = ingestion.IngestionRepository._record_run_start

    def record_start(self, run_id, resolved, started_at):
        original_start(self, run_id, resolved, started_at)
        if started is not None:
            started(run_id)

    with (
        patch.object(
            ingestion,
            "resolve_ticker",
            return_value=ResolvedTicker(ticker=ticker, cik=cik, company_name=evidence[0]["name"]),
        ),
        patch.object(ingestion, "fetch_all_submissions", return_value=evidence[0]),
        patch.object(ingestion, "fetch_company_facts", return_value=evidence[1]),
        patch.object(ingestion.IngestionRepository, "_record_run_start", record_start),
    ):
        if boundary is None:
            return ingestion.IngestionRepository(db).ingest_ticker(None, ticker)
        with patch.object(ingestion.IngestionRepository, "_commit", staticmethod(boundary)):
            return ingestion.IngestionRepository(db).ingest_ticker(None, ticker)


def probe_specs(run_id, fact_ids):
    """Static SQL, with manifest-derived bound parameters, including PIT edges."""
    probes = {
        "target_cik_first": ("financial_facts", "cik = ?", [TARGET], True),
        "other_cik": ("financial_facts", "cik = ?", [OTHER], True),
        "other_issuer": ("issuers", "cik = ?", [OTHER], True),
        "other_securities": ("securities", "cik = ?", [OTHER], True),
        "other_filings": ("filings", "cik = ?", [OTHER], True),
        "other_runs": ("ingestion_runs", "cik = ?", [OTHER], False),
        "candidate_run": ("ingestion_runs", "run_id = ?", [run_id], True),
        "issuer": ("issuers", "cik = ?", [TARGET], True),
        "ticker": ("securities", "ticker = ?", ["SYNTH"], True),
        "historical_class": ("securities", "ticker = ?", ["SYNTH.A"], True),
        "new_class": ("securities", "ticker = ?", ["SYNTH.B"], False),
        "filing": ("filings", "accession_number = ?", [f"{TARGET}-26-000001"], True),
    }
    for i, fact_id in enumerate(fact_ids):
        probes[f"fact_id_{i}"] = ("financial_facts", "fact_id = ?", [fact_id], False)
    for name, timestamp in (
        ("pit_before", "2026-01-02T20:59:59.999999+00:00"),
        ("pit_old_at", "2026-01-02T21:00:00+00:00"),
        ("pit_new_at", "2026-01-04T21:00:00+00:00"),
    ):
        probes[name] = (
            "financial_facts",
            "cik = ? AND available_at <= ?",
            [TARGET, datetime.fromisoformat(timestamp)],
            False,
        )
    return {
        name: {
            "sql": f"SELECT * FROM {table} WHERE {where} ORDER BY {TABLE_KEYS[table]}",
            "params": params,
            "table": table,
            "where": where,
            "require_index": required,
        }
        for name, (table, where, params, required) in probes.items()
    }


def state(conn, specs):
    return {
        "probes": {name: digest_query(conn, q["sql"], q["params"]) for name, q in specs.items()},
        "tables": {
            name: digest_query(conn, f"SELECT * FROM {name} ORDER BY {key}")
            for name, key in TABLE_KEYS.items()
        },
    }


def index_nodes(profile):
    found = []
    if profile.get("extra_info", {}).get("Type") == "Index Scan":
        found.append(profile)
    for child in profile.get("children", []):
        found.extend(index_nodes(child))
    return found


def profiled_query(conn, sql, params, destination, *, index_table=None, sequential=False):
    """Profile this very SELECT, rather than executing EXPLAIN ANALYZE first."""
    if destination.exists():
        raise RecoveryMismatch(f"Refusing stale query profile: {destination}")
    conn.execute("SET profiling_output = ?", [str(destination)])
    observed = digest_query(conn, sql, params)
    if not destination.exists():
        raise RecoveryMismatch("Actual-query profiling did not produce evidence")
    profile = json.loads(destination.read_text(encoding="utf-8"))
    if profile.get("query_name") != sql:
        raise RecoveryMismatch("Profile does not describe the observed SELECT")
    indexes = index_nodes(profile)
    if index_table and not any(
        node["extra_info"].get("Table", "").rsplit(".", 1)[-1] == index_table for node in indexes
    ):
        raise RecoveryMismatch(f"Required actual Index Scan absent: {index_table}")
    if sequential and indexes:
        raise RecoveryMismatch("Full-scan oracle unexpectedly used an index")
    return observed


def compare_views(indexed, scanned):
    if indexed != scanned:
        raise RecoveryMismatch("Selective indexed reads disagree with scan-forced reads")


def matches_probe(query, row):
    for field, value in zip(query["where"].split(" AND "), query["params"], strict=True):
        if field.endswith(" <= ?"):
            if row[field.removesuffix(" <= ?")] > value:
                return False
        elif row[field.removesuffix(" = ?")] != value:
            return False
    return True


def scan_oracle(conn, specs, profiles):
    """Derive selective results from unfiltered, physically scanned rows."""
    tables, probes = {}, {}
    for table, key in TABLE_KEYS.items():
        sql = f"SELECT * FROM {table} ORDER BY {key}"
        destination = profiles / f"full-{table}.json"
        if destination.exists():
            raise RecoveryMismatch("Refusing stale full-scan profile")
        conn.execute("SET profiling_output = ?", [str(destination)])
        cursor = conn.execute(sql)
        columns = [item[0] for item in cursor.description]
        selected = {name: q for name, q in specs.items() if q["table"] == table}
        hashes = {
            name: hashlib.sha256(json.dumps(columns).encode()) for name in ["table", *selected]
        }
        counts = dict.fromkeys(hashes, 0)
        while rows := cursor.fetchmany(500):
            for row in rows:
                encoded = (json.dumps([canonical(v) for v in row], sort_keys=True) + "\n").encode()
                mapped = dict(zip(columns, row, strict=True))
                names = ["table", *[n for n, q in selected.items() if matches_probe(q, mapped)]]
                for name in names:
                    hashes[name].update(encoded)
                    counts[name] += 1
        profile = json.loads(destination.read_text(encoding="utf-8"))
        if profile.get("query_name") != sql or index_nodes(profile):
            raise RecoveryMismatch("Full table query was not an independent scan")
        for name in hashes:
            result = {"columns": columns, "rows": counts[name], "sha256": hashes[name].hexdigest()}
            if name == "table":
                tables[table] = result
            else:
                probes[name] = result
    return tables, probes


def verify_first_reads(directory, manifest, label="recovery"):
    """First data SELECT uses the CIK index; never initialize, repair or reconcile."""
    import duckdb

    specs = probe_specs(manifest["run_id"], manifest["fact_ids"])
    output = directory / f"reads-{label}"
    output.mkdir(exist_ok=False)
    profiles = output / "profiles"
    profiles.mkdir()
    conn = duckdb.connect(str(directory / "synthetic.duckdb"))
    try:
        conn.execute("SET TimeZone='UTC'")
        # Diagnostic planner threshold, recorded explicitly. No index rebuild,
        # checkpoint, memory setting or production configuration workaround.
        conn.execute("SET index_scan_max_count=1000000")
        conn.execute("PRAGMA enable_profiling='json'")
        indexed = {}
        for name, q in specs.items():
            indexed[name] = profiled_query(
                conn,
                q["sql"],
                q["params"],
                profiles / f"index-{name}.json",
                index_table=q["table"] if q["require_index"] else None,
            )
        write_json(output / "index-first.json", indexed)
        conn.execute("SET index_scan_max_count=0")
        conn.execute("SET index_scan_percentage=0")
        tables, scanned = scan_oracle(conn, specs, profiles)
        write_json(output / "scan-probes.json", scanned)
        observed = {"probes": indexed, "tables": tables}
        write_json(output / "observed.json", observed)
        compare_views(indexed, scanned)
        matches = [name for name in ("old", "new") if manifest[name] == observed]
        if len(matches) != 1:
            raise RecoveryMismatch("Not exactly one complete old/new state, including run status")
        return matches[0]
    finally:
        conn.close()
