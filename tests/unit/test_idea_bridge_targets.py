"""Unit test for IdeaBridge target-RR configurability.

Verifies that IdeaBridge reads target multiples from ScannerConfig at
construction (post-2026-04-25 change) and that defaults match the
backtested-positive config. Catches regressions where someone might
revert to a hardcoded 2.5R/3.0R.
"""
from pathlib import Path

import pytest

from signal_scanner.config import ScannerConfig
from signal_scanner.database.db_manager import DatabaseManager
from signal_scanner.paper.idea_bridge import IdeaBridge
from signal_scanner.paper.paper_trader import PaperTrader


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    db_path = tmp_path / "test.db"
    mgr = DatabaseManager(db_path=str(db_path))
    mgr.init_db()
    return mgr


def test_idea_bridge_uses_default_1R_target(db):
    """Default config should produce 1R primary / 1.5R stretch — backtested
    on Triple Lock universe at 2*ATR stops to be net-positive after costs."""
    cfg = ScannerConfig()
    pt = PaperTrader(db, cfg)
    bridge = IdeaBridge(pt, db)
    assert bridge.TARGET_RR == 1.0, "default primary R-multiple must be 1.0 (was 2.5)"
    assert bridge.STRETCH_RR == 1.5, "default stretch R-multiple must be 1.5 (was 3.0)"


def test_idea_bridge_respects_config_override(db):
    """A/B testing or alternate-strategy users can override the multiples."""
    cfg = ScannerConfig()
    cfg.paper_idea_target_r_multiple = 2.0
    cfg.paper_idea_stretch_target_r_multiple = 3.5
    pt = PaperTrader(db, cfg)
    bridge = IdeaBridge(pt, db)
    assert bridge.TARGET_RR == 2.0
    assert bridge.STRETCH_RR == 3.5


def test_config_field_types_are_numeric(db):
    cfg = ScannerConfig()
    assert isinstance(cfg.paper_idea_target_r_multiple, (int, float))
    assert isinstance(cfg.paper_idea_stretch_target_r_multiple, (int, float))
    assert cfg.paper_idea_target_r_multiple > 0
    assert cfg.paper_idea_stretch_target_r_multiple >= cfg.paper_idea_target_r_multiple
