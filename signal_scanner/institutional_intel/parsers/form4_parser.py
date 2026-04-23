"""Form 4 parser scaffolding."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List
import xml.etree.ElementTree as ET


def _extract_insider_role(root: ET.Element) -> str:
    """Extract insider role from <reportingOwnerRelationship> XML node.

    Maps SEC boolean flags to the standard role values used in
    fact_form4_transactions: Officer, Director, TenPercentOwner, Other.
    When multiple flags are true, priority: Officer > Director > TenPercentOwner > Other.
    """
    rel = root.find(".//reportingOwnerRelationship")
    if rel is None:
        return ""
    is_officer = (rel.findtext("isOfficer") or "").strip().lower() == "true"
    is_director = (rel.findtext("isDirector") or "").strip().lower() == "true"
    is_ten_pct = (rel.findtext("isTenPercentOwner") or "").strip().lower() == "true"
    is_other = (rel.findtext("isOther") or "").strip().lower() == "true"

    if is_officer:
        return "Officer"
    if is_director:
        return "Director"
    if is_ten_pct:
        return "TenPercentOwner"
    if is_other:
        return "Other"

    # Fallback: infer from officerTitle if boolean flags are missing
    title = (rel.findtext("officerTitle") or "").strip()
    if title:
        return "Officer"
    return ""


def parse_form4(xml_path: Path, context: Dict[str, str]) -> List[Dict]:
    """Parse Form 4 XML into normalized transaction rows."""
    rows: List[Dict] = []
    root = ET.fromstring(xml_path.read_text(encoding="utf-8", errors="ignore"))
    now_iso = datetime.now(timezone.utc).isoformat()

    issuer_cik = (root.findtext(".//issuerCik") or "").strip()
    issuer_name = (root.findtext(".//issuerName") or "").strip()
    issuer_ticker = (root.findtext(".//issuerTradingSymbol") or "").strip()
    insider_name = (root.findtext(".//rptOwnerName") or "").strip()
    insider_role = _extract_insider_role(root)

    # Focus on non-derivative transactions first.
    tx_nodes = root.findall(".//nonDerivativeTransaction")
    for tx in tx_nodes:
        code = (tx.findtext(".//transactionCoding/transactionCode") or "").strip().upper()
        shares_s = (tx.findtext(".//transactionAmounts/transactionShares/value") or "").strip()
        price_s = (tx.findtext(".//transactionAmounts/transactionPricePerShare/value") or "").strip()
        owned_s = (
            tx.findtext(".//postTransactionAmounts/sharesOwnedFollowingTransaction/value") or ""
        ).strip()
        tx_date = (tx.findtext(".//transactionDate/value") or "").strip()
        direction = "BUY" if code == "P" else ("SELL" if code == "S" else "OTHER")

        rows.append(
            {
                "filing_accession_no": context.get("accession_no"),
                "issuer_cik": issuer_cik,
                "issuer_name": issuer_name,
                "ticker": issuer_ticker,
                "insider_name": insider_name,
                "insider_role": insider_role,
                "transaction_date": tx_date or None,
                "transaction_code": code,
                "direction": direction,
                "shares": float(shares_s or 0.0),
                "price": float(price_s or 0.0),
                "ownership_after": float(owned_s or 0.0),
                "source_path": str(xml_path),
                "ingested_at": now_iso,
            }
        )
    return rows

