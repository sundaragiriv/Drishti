"""SEC daily index ingestion helpers."""

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, List

from loguru import logger

from signal_scanner.institutional_intel.ingest.sec_client import SecClient


@dataclass
class FilingIndexEntry:
    cik: str
    company_name: str
    form_type: str
    filing_date: str
    file_name: str

    @property
    def filing_url(self) -> str:
        return f"https://www.sec.gov/Archives/{self.file_name}"

    @property
    def accession_no(self) -> str:
        return _derive_accession_no(self.file_name)


def iter_dates(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def quarter_of(d: date) -> int:
    return ((d.month - 1) // 3) + 1


def master_index_url(d: date) -> str:
    ymd = d.strftime("%Y%m%d")
    qtr = quarter_of(d)
    return f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{qtr}/master.{ymd}.idx"


def quarterly_master_index_url(year: int, qtr: int) -> str:
    return f"https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{qtr}/master.idx"


def fetch_daily_index(client: SecClient, d: date) -> List[FilingIndexEntry]:
    """Fetch and parse SEC daily master index for a date."""
    url = master_index_url(d)
    try:
        text = client.get_text(url)
    except Exception as ex:
        # Many days have no index (weekends/holidays). Keep this low-noise.
        logger.debug(f"No daily index for {d.isoformat()} ({ex})")
        return []
    return parse_master_index_text(text)


def iter_quarters(start: date, end: date) -> Iterable[tuple[int, int]]:
    y, q = start.year, quarter_of(start)
    end_y, end_q = end.year, quarter_of(end)
    while (y < end_y) or (y == end_y and q <= end_q):
        yield y, q
        q += 1
        if q > 4:
            q = 1
            y += 1


def fetch_quarterly_index(client: SecClient, year: int, qtr: int) -> List[FilingIndexEntry]:
    """Fetch and parse SEC quarterly master index."""
    text = fetch_quarterly_index_text(client, year, qtr)
    if not text:
        return []
    return parse_master_index_text(text)


def fetch_quarterly_index_text(client: SecClient, year: int, qtr: int) -> str:
    """Fetch quarterly master index raw text payload."""
    url = quarterly_master_index_url(year, qtr)
    try:
        return client.get_text(url)
    except Exception as ex:
        logger.debug(f"No quarterly index for {year} Q{qtr} ({ex})")
        return ""


def parse_master_index_text(text: str) -> List[FilingIndexEntry]:
    """Parse master.idx payload into structured filing entries."""
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("-----"):
            start = i + 1
            break

    out: List[FilingIndexEntry] = []
    for raw in lines[start:]:
        parts = raw.split("|")
        if len(parts) != 5:
            continue
        cik, company, form_type, filed_at, file_name = [p.strip() for p in parts]
        out.append(
            FilingIndexEntry(
                cik=cik,
                company_name=company,
                form_type=form_type.upper(),
                filing_date=filed_at,
                file_name=file_name,
            )
        )
    return out


def _derive_accession_no(file_name: str) -> str:
    p = Path(file_name)
    stem = p.stem
    if stem.count("-") == 2:
        return stem

    parent = p.parent.name
    if parent.isdigit() and len(parent) == 18:
        # 000032019324000058 -> 0000320193-24-000058
        return f"{parent[:10]}-{parent[10:12]}-{parent[12:]}"
    return stem or parent or file_name.replace("/", "_")
