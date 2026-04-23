"""Shared download loop for SEC filing backfill/incremental jobs."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import duckdb
from loguru import logger

from signal_scanner.institutional_intel.config import InstitutionalIntelConfig, META_DIR, RAW_SEC_DIR
from signal_scanner.institutional_intel.ingest.manifest import (
    upsert_manifest_record,
    upsert_manifest_records,
)
from signal_scanner.institutional_intel.ingest.sec_client import SecClient
from signal_scanner.institutional_intel.ingest.sec_index import (
    FilingIndexEntry,
    fetch_quarterly_index,
    fetch_quarterly_index_text,
    iter_quarters,
    quarter_of,
)

INSIDER_FORMS = {"3", "4", "5"}
_THREAD_LOCAL = threading.local()


def _normalize_cik(value: str) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    try:
        return str(int(s))
    except ValueError:
        return s.lstrip("0")


def download_filing_raw(
    client: SecClient,
    entry: FilingIndexEntry,
    force: bool = False,
) -> Path:
    """Download raw filing payload and return local path."""
    filed_dt = datetime.fromisoformat(entry.filing_date).date()
    form_tag = entry.form_type.replace("/", "_")
    out_dir = (
        RAW_SEC_DIR
        / f"form_type={form_tag}"
        / f"year={filed_dt.year}"
        / f"quarter=Q{quarter_of(filed_dt)}"
        / f"cik={entry.cik}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{entry.accession_no}.txt"

    if out_path.exists() and not force:
        return out_path

    payload = client.get_text(entry.filing_url)
    out_path.write_text(payload, encoding="utf-8", errors="ignore")
    return out_path


def _metadata_manifest_path(entry: FilingIndexEntry) -> str:
    filed_dt = datetime.fromisoformat(entry.filing_date).date()
    form_tag = entry.form_type.replace("/", "_")
    return (
        "manifest://sec/metadata/"
        f"form_type={form_tag}/year={filed_dt.year}/quarter=Q{quarter_of(filed_dt)}"
        f"/cik={entry.cik}/{entry.accession_no}"
    )


def _load_universe_symbols(universe_file: str) -> Set[str]:
    p = Path(universe_file)
    if not p.exists():
        raise FileNotFoundError(f"Universe file not found: {p}")
    out: Set[str] = set()
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip().upper()
        if not s or s.startswith("#"):
            continue
        out.add(s)
    return out


def _load_ticker_to_cik_map(client: SecClient) -> Dict[str, str]:
    """Load SEC ticker-to-CIK map (single call, reused across run)."""
    url = "https://www.sec.gov/files/company_tickers_exchange.json"
    payload = client.get_json(url)
    data = payload.get("data", [])
    out: Dict[str, str] = {}
    for row in data:
        # Format observed: [cik, name, ticker, exchange]
        if not isinstance(row, list) or len(row) < 3:
            continue
        cik_raw = row[0]
        ticker = str(row[2] or "").strip().upper()
        cik = _normalize_cik(cik_raw)
        if ticker and cik:
            out[ticker] = cik
    return out


def _build_universe_ciks(client: SecClient, universe_file: str) -> Set[str]:
    symbols = _load_universe_symbols(universe_file)
    t2c = _load_ticker_to_cik_map(client)
    ciks = {_normalize_cik(t2c[s]) for s in symbols if s in t2c}
    missing = len(symbols) - len(ciks)
    logger.info(
        "Universe mapping loaded | symbols={} | mapped_ciks={} | unmapped_symbols={}",
        len(symbols),
        len(ciks),
        missing,
    )
    return {c for c in ciks if c}


def _entry_allowed(entry: FilingIndexEntry, selected_forms: Set[str], universe_ciks: Set[str]) -> bool:
    if entry.form_type not in selected_forms:
        return False
    if universe_ciks and entry.form_type in INSIDER_FORMS:
        return _normalize_cik(entry.cik) in universe_ciks
    return True


def _quarter_index_cache_path(year: int, qtr: int) -> Path:
    return META_DIR / "sec_indexes" / f"{year}_Q{qtr}_master.idx"


def _get_quarter_index_path(client: SecClient, year: int, qtr: int) -> Optional[Path]:
    """Ensure local cached quarterly master index exists and return its path."""
    path = _quarter_index_cache_path(year, qtr)
    if path.exists():
        return path
    text = fetch_quarterly_index_text(client, year, qtr)
    if not text:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", errors="ignore")
    return path


def _filter_quarter_entries_with_duckdb(
    *,
    index_path: Path,
    selected_forms: Set[str],
    start_date: date,
    end_date: date,
    universe_ciks: Set[str],
) -> Tuple[List[FilingIndexEntry], int]:
    """
    Filter quarterly index with DuckDB (fast pre-filter) and return eligible entries.

    Returns: (eligible_entries, insider_skipped_by_universe_for_quarter)
    """
    if not index_path.exists() or not selected_forms:
        return [], 0

    conn = duckdb.connect()
    try:
        conn.execute(
            """
            CREATE TEMP TABLE idx AS
            SELECT
                trim(cik) AS cik,
                trim(company_name) AS company_name,
                upper(trim(form_type)) AS form_type,
                CAST(trim(filing_date) AS DATE) AS filing_date,
                trim(file_name) AS file_name
            FROM read_csv(
                ?,
                delim='|',
                header=false,
                skip=11,
                columns={
                    'cik': 'VARCHAR',
                    'company_name': 'VARCHAR',
                    'form_type': 'VARCHAR',
                    'filing_date': 'VARCHAR',
                    'file_name': 'VARCHAR'
                }
            )
            """,
            [str(index_path)],
        )

        form_list = sorted({str(f).upper() for f in selected_forms if str(f).strip()})
        form_values = ", ".join([f"'{f}'" for f in form_list])
        date_start = start_date.isoformat()
        date_end = end_date.isoformat()
        cik_norm_expr = "CASE WHEN ltrim(cik, '0') = '' THEN '0' ELSE ltrim(cik, '0') END"

        skipped = 0
        if universe_ciks:
            conn.execute("CREATE TEMP TABLE universe_ciks(cik_norm VARCHAR)")
            conn.executemany(
                "INSERT INTO universe_ciks(cik_norm) VALUES (?)",
                [(c,) for c in sorted(universe_ciks)],
            )
            skipped = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM idx
                    WHERE form_type IN ('3', '4', '5')
                      AND filing_date BETWEEN ?::DATE AND ?::DATE
                      AND {cik_norm_expr} NOT IN (SELECT cik_norm FROM universe_ciks)
                    """,
                    [date_start, date_end],
                ).fetchone()[0]
                or 0
            )
            rows = conn.execute(
                f"""
                SELECT cik, company_name, form_type, CAST(filing_date AS VARCHAR), file_name
                FROM idx
                WHERE form_type IN ({form_values})
                  AND filing_date BETWEEN ?::DATE AND ?::DATE
                  AND (
                        form_type NOT IN ('3', '4', '5')
                        OR {cik_norm_expr} IN (SELECT cik_norm FROM universe_ciks)
                  )
                ORDER BY filing_date, cik
                """,
                [date_start, date_end],
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT cik, company_name, form_type, CAST(filing_date AS VARCHAR), file_name
                FROM idx
                WHERE form_type IN ({form_values})
                  AND filing_date BETWEEN ?::DATE AND ?::DATE
                ORDER BY filing_date, cik
                """,
                [date_start, date_end],
            ).fetchall()

        entries = [
            FilingIndexEntry(
                cik=str(r[0]).strip(),
                company_name=str(r[1]).strip(),
                form_type=str(r[2]).strip().upper(),
                filing_date=str(r[3]).strip()[:10],
                file_name=str(r[4]).strip(),
            )
            for r in rows
            if r and r[0] and r[4]
        ]
        return entries, skipped
    finally:
        conn.close()


def _bulk_upsert_metadata_entries(entries: Sequence[FilingIndexEntry], progress_every: int = 500) -> int:
    """Persist metadata-only manifest rows in batches for much higher throughput."""
    if not entries:
        return 0
    saved = 0
    batch: List[dict] = []
    batch_size = 5000
    for e in entries:
        batch.append(
            {
                "accession_no": e.accession_no,
                "form_type": e.form_type,
                "cik": e.cik,
                "filing_date": e.filing_date,
                "source_url": e.filing_url,
                "local_path": _metadata_manifest_path(e),
                "sha256": None,
            }
        )
        if len(batch) >= batch_size:
            saved += upsert_manifest_records(batch)
            batch.clear()
    if batch:
        saved += upsert_manifest_records(batch)
    return saved


def _process_entry(
    entry: FilingIndexEntry,
    *,
    user_agent: str,
    requests_per_second: float,
    force: bool,
    metadata_only: bool,
) -> Tuple[bool, Optional[str]]:
    client = _get_thread_client(
        user_agent=user_agent,
        requests_per_second=requests_per_second,
    )
    try:
        local_path = _metadata_manifest_path(entry) if metadata_only else download_filing_raw(client, entry, force=force)
        upsert_manifest_record(
            accession_no=entry.accession_no,
            form_type=entry.form_type,
            cik=entry.cik,
            filing_date=entry.filing_date,
            source_url=entry.filing_url,
            local_path=local_path,
        )
        return True, None
    except Exception as ex:
        return False, str(ex)


def _get_thread_client(user_agent: str, requests_per_second: float) -> SecClient:
    """Reuse one SEC client/session per worker thread."""
    key = (str(user_agent or ""), float(requests_per_second or 0.0))
    cur_key = getattr(_THREAD_LOCAL, "sec_client_key", None)
    cur_client = getattr(_THREAD_LOCAL, "sec_client", None)
    if cur_client is not None and cur_key == key:
        return cur_client

    cfg = InstitutionalIntelConfig()
    if user_agent:
        cfg.user_agent = user_agent
    if requests_per_second and requests_per_second > 0:
        cfg.requests_per_second = float(requests_per_second)
    client = SecClient(cfg)
    _THREAD_LOCAL.sec_client = client
    _THREAD_LOCAL.sec_client_key = key
    return client


def run_download_loop(
    start_date: date,
    end_date: date,
    forms: Sequence[str],
    max_filings: int = 0,
    force: bool = False,
    user_agent: str = "",
    progress_every: int = 100,
    metadata_only: bool = False,
    workers: int = 1,
    universe_file: str = "",
    requests_per_second: float = 0.0,
) -> Dict[str, int]:
    """Download SEC filings for selected forms and update manifest."""
    selected = {f.strip().upper() for f in forms if str(f).strip()}
    cfg = InstitutionalIntelConfig()
    if user_agent:
        cfg.user_agent = user_agent
    if requests_per_second and requests_per_second > 0:
        cfg.requests_per_second = float(requests_per_second)
    seed_client = SecClient(cfg)

    universe_ciks: Set[str] = set()
    if universe_file:
        universe_ciks = _build_universe_ciks(seed_client, universe_file)

    quarters_scanned = 0
    filings_seen = 0
    files_written = 0
    errors = 0
    insider_skipped_by_universe = 0

    quarters = list(iter_quarters(start_date, end_date))
    progress_file = META_DIR / "sec_download_progress.json"
    _write_progress(
        progress_file,
        {
            "state": "STARTED",
            "from_date": start_date.isoformat(),
            "to_date": end_date.isoformat(),
            "forms": sorted(selected),
            "quarters_total": len(quarters),
            "quarters_scanned": 0,
            "filings_seen": 0,
            "files_written": 0,
            "errors": 0,
            "insider_skipped_by_universe": 0,
            "metadata_only": bool(metadata_only),
            "workers": max(1, int(workers)),
            "rps": float(cfg.requests_per_second),
            "updated_at": datetime.utcnow().isoformat() + "Z",
        },
    )
    logger.info(
        "SEC download start | from={} | to={} | quarters={} | forms={} | max_filings={} | workers={} | metadata_only={} | universe_filter={}",
        start_date,
        end_date,
        len(quarters),
        sorted(selected),
        max_filings or "unlimited",
        max(1, int(workers)),
        bool(metadata_only),
        bool(universe_file),
    )

    max_workers = max(1, int(workers))
    for idx, (year, qtr) in enumerate(quarters, start=1):
        quarters_scanned += 1
        logger.info("Scanning quarter {}/{} -> {} Q{}", idx, len(quarters), year, qtr)
        index_path = _get_quarter_index_path(seed_client, year, qtr)
        if not index_path:
            logger.info("No index entries returned for {} Q{}", year, qtr)
            continue

        eligible: List[FilingIndexEntry]
        skipped_for_quarter: int
        try:
            eligible, skipped_for_quarter = _filter_quarter_entries_with_duckdb(
                index_path=index_path,
                selected_forms=selected,
                start_date=start_date,
                end_date=end_date,
                universe_ciks=universe_ciks,
            )
            insider_skipped_by_universe += skipped_for_quarter
        except Exception as ex:
            logger.warning(
                "DuckDB pre-filter failed for {} Q{} ({}); falling back to in-memory parser",
                year,
                qtr,
                ex,
            )
            entries = fetch_quarterly_index(seed_client, year, qtr)
            if not entries:
                logger.info("No index entries returned for {} Q{}", year, qtr)
                continue
            eligible = []
            for e in entries:
                if not _entry_allowed(e, selected, universe_ciks):
                    if universe_ciks and e.form_type in INSIDER_FORMS:
                        insider_skipped_by_universe += 1
                    continue
                try:
                    filing_day = datetime.fromisoformat(e.filing_date).date()
                except Exception:
                    continue
                if filing_day < start_date or filing_day > end_date:
                    continue
                eligible.append(e)

        if max_filings > 0:
            remaining = max(0, max_filings - filings_seen)
            if remaining <= 0:
                break
            eligible = eligible[:remaining]

        if not eligible:
            logger.info("Quarter done {}/{} -> {} Q{} | no eligible entries", idx, len(quarters), year, qtr)
            continue

        logger.info(
            "Quarter {} Q{} eligible={} | insider_skipped_by_universe_total={}",
            year,
            qtr,
            len(eligible),
            insider_skipped_by_universe,
        )

        if metadata_only:
            if max_filings > 0:
                remaining = max(0, max_filings - filings_seen)
                if remaining <= 0:
                    break
                eligible = eligible[:remaining]
            if not eligible:
                logger.info("Quarter done {}/{} -> {} Q{} | no eligible entries", idx, len(quarters), year, qtr)
                continue

            filings_seen += len(eligible)
            files_written += _bulk_upsert_metadata_entries(
                eligible,
                progress_every=max(1, int(progress_every)),
            )
            logger.info(
                "Quarter done {}/{} -> {} Q{} | seen_so_far={} | saved_so_far={} | errors_so_far={} (metadata-batch)",
                idx,
                len(quarters),
                year,
                qtr,
                filings_seen,
                files_written,
                errors,
            )
            _write_progress(
                progress_file,
                {
                    "state": "RUNNING",
                    "quarter_index": idx,
                    "quarters_total": len(quarters),
                    "quarters_scanned": quarters_scanned,
                    "filings_seen": filings_seen,
                    "files_written": files_written,
                    "errors": errors,
                    "insider_skipped_by_universe": insider_skipped_by_universe,
                    "updated_at": datetime.utcnow().isoformat() + "Z",
                },
            )
            continue

        if max_workers == 1:
            for e in eligible:
                filings_seen += 1
                ok, err_msg = _process_entry(
                    e,
                    user_agent=user_agent,
                    requests_per_second=cfg.requests_per_second,
                    force=force,
                    metadata_only=metadata_only,
                )
                if ok:
                    files_written += 1
                else:
                    errors += 1
                    logger.warning(
                        "Failed filing download | form={} | cik={} | filing_date={} | accession={} | err={}",
                        e.form_type,
                        e.cik,
                        e.filing_date,
                        e.accession_no,
                        err_msg,
                    )
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(
                        _process_entry,
                        e,
                        user_agent=user_agent,
                        requests_per_second=cfg.requests_per_second,
                        force=force,
                        metadata_only=metadata_only,
                    ): e
                    for e in eligible
                }
                for fut in as_completed(futures):
                    e = futures[fut]
                    filings_seen += 1
                    try:
                        ok, err_msg = fut.result()
                    except Exception as ex:
                        ok, err_msg = False, str(ex)
                    if ok:
                        files_written += 1
                    else:
                        errors += 1
                        logger.warning(
                            "Failed filing download | form={} | cik={} | filing_date={} | accession={} | err={}",
                            e.form_type,
                            e.cik,
                            e.filing_date,
                            e.accession_no,
                            err_msg,
                        )

        logger.info(
            "Quarter done {}/{} -> {} Q{} | seen_so_far={} | saved_so_far={} | errors_so_far={}",
            idx,
            len(quarters),
            year,
            qtr,
            filings_seen,
            files_written,
            errors,
        )
        _write_progress(
            progress_file,
            {
                "state": "RUNNING",
                "quarter_index": idx,
                "quarters_total": len(quarters),
                "quarters_scanned": quarters_scanned,
                "filings_seen": filings_seen,
                "files_written": files_written,
                "errors": errors,
                "insider_skipped_by_universe": insider_skipped_by_universe,
                "updated_at": datetime.utcnow().isoformat() + "Z",
            },
        )

    final = {
        "quarters_scanned": quarters_scanned,
        "filings_seen": filings_seen,
        "files_written": files_written,
        "errors": errors,
        "insider_skipped_by_universe": insider_skipped_by_universe,
    }
    _write_progress(
        progress_file,
        {
            "state": "DONE",
            **final,
            "quarters_total": len(quarters),
            "updated_at": datetime.utcnow().isoformat() + "Z",
        },
    )
    return final


def _write_progress(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=True), encoding="utf-8")
    tmp.replace(path)
