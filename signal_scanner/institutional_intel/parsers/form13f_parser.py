"""13F parser scaffolding.

Phase A goal: establish parser contracts and normalized output shape.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List
import xml.etree.ElementTree as ET


def parse_13f_information_table(xml_path: Path, context: Dict[str, str]) -> List[Dict]:
    """Parse 13F informationTable XML into normalized rows.

    Args:
        xml_path: Path to filing XML payload containing holdings table.
        context: Filing-level metadata (accession, manager_cik/name, report_period, filed_at).
    """
    rows: List[Dict] = []
    root = ET.fromstring(xml_path.read_text(encoding="utf-8", errors="ignore"))
    now_iso = datetime.now(timezone.utc).isoformat()

    ns = {"n": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
    info_nodes = root.findall(".//n:infoTable", ns) if ns else root.findall(".//infoTable")

    for n in info_nodes:
        def txt(path: str) -> str:
            t = n.findtext(path, default="", namespaces=ns) if ns else n.findtext(path, default="")
            return (t or "").strip()

        rows.append(
            {
                "filing_accession_no": context.get("accession_no"),
                "manager_cik": context.get("manager_cik"),
                "manager_name": context.get("manager_name"),
                "report_period": context.get("report_period"),
                "filed_at": context.get("filed_at"),
                "issuer_name": txt("n:nameOfIssuer" if ns else "nameOfIssuer"),
                "cusip": txt("n:cusip" if ns else "cusip"),
                "ticker": "",
                "class_title": txt("n:titleOfClass" if ns else "titleOfClass"),
                "value_usd_thousands": float(txt("n:value" if ns else "value") or 0.0),
                "shares": float(
                    txt("n:shrsOrPrnAmt/n:sshPrnamt" if ns else "shrsOrPrnAmt/sshPrnamt") or 0.0
                ),
                "put_call": txt("n:putCall" if ns else "putCall"),
                "discretion": txt("n:investmentDiscretion" if ns else "investmentDiscretion"),
                "source_path": str(xml_path),
                "ingested_at": now_iso,
            }
        )
    return rows

