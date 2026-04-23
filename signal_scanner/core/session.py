"""Session registry and control plane for Quant-Bridge.

Provides:
  - Single-session protection (refuse duplicate live/EOD/premarket sessions)
  - Session registry with ownership tracking
  - Explicit phase/state machine (EOD_REFRESH → OVERNIGHT_IDLE → PREMARKET_PREP →
    PREOPEN_VALIDATION → LIVE_EXECUTION → POSTCLOSE_REVIEW)
  - Job conflict prevention
  - Operator visibility into current phase / active job / blocked jobs

Session state is persisted to a JSON lock file so multiple processes can
coordinate. On Windows, DuckDB only allows one write connection, so the
session registry also serves as a coordination mechanism for DB access.

Usage:
    from signal_scanner.core.session import SessionRegistry, SessionMode

    reg = SessionRegistry()
    acquired = reg.acquire(SessionMode.LIVE_EXECUTION, owner="scanner")
    if not acquired:
        print(reg.refusal_message())
        sys.exit(1)
    # ... do work ...
    reg.release()
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional


SESSION_FILE = Path("data/warehouse/session.json")


class SessionMode(str, Enum):
    """Mutually exclusive session modes."""
    EOD_REFRESH = "EOD_REFRESH"
    OVERNIGHT_IDLE = "OVERNIGHT_IDLE"
    PREMARKET_PREP = "PREMARKET_PREP"
    PREOPEN_VALIDATION = "PREOPEN_VALIDATION"
    LIVE_EXECUTION = "LIVE_EXECUTION"
    POSTCLOSE_REVIEW = "POSTCLOSE_REVIEW"


class SessionPhase(str, Enum):
    """Operational phases within a session."""
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# Which modes conflict with each other (cannot run simultaneously)
_CONFLICTS = {
    SessionMode.LIVE_EXECUTION: {
        SessionMode.LIVE_EXECUTION,       # no duplicate scanner
        SessionMode.EOD_REFRESH,          # EOD writes to DuckDB
        SessionMode.PREMARKET_PREP,       # premarket writes to DuckDB
    },
    SessionMode.EOD_REFRESH: {
        SessionMode.EOD_REFRESH,          # no duplicate EOD
        SessionMode.LIVE_EXECUTION,       # scanner reads DuckDB
        SessionMode.PREMARKET_PREP,       # both write to DuckDB
    },
    SessionMode.PREMARKET_PREP: {
        SessionMode.PREMARKET_PREP,       # no duplicate premarket
        SessionMode.LIVE_EXECUTION,       # scanner reads DuckDB
        SessionMode.EOD_REFRESH,          # both write to DuckDB
    },
    SessionMode.PREOPEN_VALIDATION: {
        SessionMode.PREOPEN_VALIDATION,   # no duplicate
    },
    SessionMode.POSTCLOSE_REVIEW: {
        SessionMode.POSTCLOSE_REVIEW,     # no duplicate
    },
}


@dataclass
class SessionRecord:
    """Persisted session state."""
    session_id: str = ""
    pid: int = 0
    mode: str = SessionMode.OVERNIGHT_IDLE.value
    phase: str = SessionPhase.COMPLETED.value
    owner: str = ""
    started_at: str = ""
    last_heartbeat: str = ""
    active_job: str = ""
    blocked_jobs: list = field(default_factory=list)
    blocked_reasons: list = field(default_factory=list)
    dashboard_port: int = 0
    ibkr_client_id: int = 0
    owned_resources: list = field(default_factory=list)

    def is_active(self) -> bool:
        return self.phase in (SessionPhase.STARTING.value, SessionPhase.RUNNING.value)

    def to_dict(self) -> dict:
        return asdict(self)


LOCK_FILE = SESSION_FILE.with_suffix(".lock")

# Heartbeat staleness threshold: if heartbeat is older than this,
# the session is considered wedged/hung even if PID is alive.
HEARTBEAT_STALE_SECONDS = 300  # 5 minutes


class SessionRegistry:
    """Single-session protection and phase management.

    Uses an OS-level exclusive file lock for atomic cross-process
    session acquisition. On Windows, msvcrt.locking provides this.
    Detects stale sessions by checking PID liveness AND heartbeat age.
    """

    def __init__(self, session_file: Path = SESSION_FILE):
        self._path = session_file
        self._lock_path = session_file.with_suffix(".lock")
        self._record: Optional[SessionRecord] = None
        self._last_refusal: Optional[SessionRecord] = None

    def acquire(
        self,
        mode: SessionMode,
        owner: str = "",
        dashboard_port: int = 0,
        ibkr_client_id: int = 0,
    ) -> bool:
        """Atomically try to acquire the session. Returns True if successful.

        Uses an OS-level file lock to prevent two concurrent starts
        from both succeeding. Refuses if a conflicting session is active
        (PID alive AND heartbeat fresh). Automatically cleans up stale
        sessions from dead or wedged processes.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # OS-level exclusive lock for atomic check-and-write
        lock_fd = self._os_lock_acquire()
        if lock_fd is None:
            # Another process is mid-acquire right now — refuse
            self._last_refusal = SessionRecord(
                mode="UNKNOWN", phase="STARTING", owner="(concurrent acquire in progress)"
            )
            return False

        try:
            existing = self._load()

            # Check for active conflicting session
            if existing and existing.is_active():
                if self._is_session_alive(existing):
                    # Check if modes conflict
                    conflicts = _CONFLICTS.get(mode, {mode})
                    if any(existing.mode == c.value for c in conflicts):
                        self._last_refusal = existing
                        return False
                # else: stale session — owning process is dead or wedged, clean up

            # Acquire
            now = datetime.now(timezone.utc).isoformat()
            self._record = SessionRecord(
                session_id=f"{mode.value}_{int(time.time())}",
                pid=os.getpid(),
                mode=mode.value,
                phase=SessionPhase.STARTING.value,
                owner=owner,
                started_at=now,
                last_heartbeat=now,
                dashboard_port=dashboard_port,
                ibkr_client_id=ibkr_client_id,
                owned_resources=self._resources_for_mode(mode),
            )
            self._save()
            return True
        finally:
            self._os_lock_release(lock_fd)

    def set_phase(self, phase: SessionPhase) -> None:
        """Update the current phase."""
        if self._record:
            self._record.phase = phase.value
            self._record.last_heartbeat = datetime.now(timezone.utc).isoformat()
            self._save()

    def start_background_heartbeat(self, interval_seconds: int = 30) -> None:
        """Start a daemon thread that updates heartbeat periodically.

        Use for long-running batch sessions (premarket, EOD) where steps
        can exceed the staleness threshold. The thread stops automatically
        when the main process exits or when release() is called.
        """
        import threading

        self._heartbeat_stop = threading.Event()

        def _beat():
            while not self._heartbeat_stop.is_set():
                self.heartbeat()
                self._heartbeat_stop.wait(interval_seconds)

        t = threading.Thread(target=_beat, daemon=True, name="session-heartbeat")
        t.start()
        self._heartbeat_thread = t

    def set_active_job(self, job: str) -> None:
        """Update the currently active job name."""
        if self._record:
            self._record.active_job = job
            self._record.last_heartbeat = datetime.now(timezone.utc).isoformat()
            self._save()

    def clear_active_job(self) -> None:
        """Clear the active job (call when a job finishes)."""
        if self._record:
            self._record.active_job = ""
            self._record.last_heartbeat = datetime.now(timezone.utc).isoformat()
            self._save()

    def record_blocked_job(self, job: str, reason: str) -> None:
        """Record a job that was blocked and why."""
        if self._record:
            entry = f"{job}: {reason}"
            if entry not in self._record.blocked_jobs:
                self._record.blocked_jobs.append(entry)
            if reason not in self._record.blocked_reasons:
                self._record.blocked_reasons.append(reason)
            self._save()

    def heartbeat(self) -> None:
        """Update the heartbeat timestamp."""
        if self._record:
            self._record.last_heartbeat = datetime.now(timezone.utc).isoformat()
            self._save()

    def release(self, phase: SessionPhase = SessionPhase.COMPLETED) -> None:
        """Release the session and stop background heartbeat if running."""
        # Stop background heartbeat thread
        stop_event = getattr(self, "_heartbeat_stop", None)
        if stop_event:
            stop_event.set()

        if self._record:
            self._record.phase = phase.value
            self._record.active_job = ""
            self._record.last_heartbeat = datetime.now(timezone.utc).isoformat()
            self._save()
            self._record = None

    def refusal_message(self) -> str:
        """Human-readable refusal message after a failed acquire()."""
        existing = getattr(self, "_last_refusal", None)
        if not existing:
            existing = self._load()
        if not existing or not existing.is_active():
            return "No conflicting session found."
        return (
            f"SESSION REFUSED: cannot start — conflicting session is active.\n"
            f"  Active session: {existing.mode} (PID {existing.pid})\n"
            f"  Owner:          {existing.owner}\n"
            f"  Started:        {existing.started_at[:19]}\n"
            f"  Phase:          {existing.phase}\n"
            f"  Active job:     {existing.active_job or 'none'}\n"
            f"  IBKR client:    {existing.ibkr_client_id}\n"
            f"  Dashboard:      port {existing.dashboard_port}\n"
            f"\n"
            f"To fix:\n"
            f"  1. Stop the existing session (Ctrl+C in its terminal), or\n"
            f"  2. If the process is dead, delete {self._path}\n"
        )

    # ----------------------------------------------------------------
    # Status queries (for operator visibility)
    # ----------------------------------------------------------------

    def get_current(self) -> Optional[dict]:
        """Return the current session state, or None if no active session."""
        rec = self._load()
        if rec and rec.is_active():
            if not self._is_session_alive(rec):
                reason = "PID dead" if not self._pid_alive(rec.pid) else "heartbeat stale"
                return {**rec.to_dict(), "_stale": True,
                        "_note": f"{reason} -- session is stale"}
            return rec.to_dict()
        return None

    def get_status_display(self) -> str:
        """Human-readable current session status."""
        rec = self._load()
        if not rec:
            return "No session registered."

        if not rec.is_active():
            return (
                f"Last session: {rec.mode} ({rec.phase})\n"
                f"  Owner:   {rec.owner}\n"
                f"  Started: {rec.started_at[:19]}\n"
                f"  Ended:   {rec.last_heartbeat[:19]}"
            )

        alive = self._pid_alive(rec.pid)
        stale_note = "" if alive else "  ** WARNING: PID is dead — session is stale **\n"

        lines = [
            f"Active session: {rec.mode}",
            f"  Session ID:  {rec.session_id}",
            f"  PID:         {rec.pid} ({'alive' if alive else 'DEAD'})",
            f"  Owner:       {rec.owner}",
            f"  Phase:       {rec.phase}",
            f"  Started:     {rec.started_at[:19]}",
            f"  Heartbeat:   {rec.last_heartbeat[:19]}",
            f"  Active job:  {rec.active_job or 'none'}",
            f"  IBKR client: {rec.ibkr_client_id}",
            f"  Dashboard:   port {rec.dashboard_port}",
        ]
        if stale_note:
            lines.insert(1, stale_note.strip())
        if rec.blocked_jobs:
            lines.append(f"  Blocked jobs ({len(rec.blocked_jobs)}):")
            for b in rec.blocked_jobs[-5:]:
                lines.append(f"    - {b}")
        return "\n".join(lines)

    # ----------------------------------------------------------------
    # Internal
    # ----------------------------------------------------------------

    def _resources_for_mode(self, mode: SessionMode) -> list:
        if mode == SessionMode.LIVE_EXECUTION:
            return ["IBKR", "signals.db", "DuckDB(read)"]
        elif mode in (SessionMode.EOD_REFRESH, SessionMode.PREMARKET_PREP):
            return ["DuckDB(write)", "signals.db"]
        return []

    def _pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def _is_session_alive(self, rec: SessionRecord) -> bool:
        """Check if a session is genuinely alive (PID exists AND heartbeat fresh)."""
        if not self._pid_alive(rec.pid):
            return False
        # Check heartbeat staleness — catches wedged/hung processes
        if rec.last_heartbeat:
            try:
                hb_time = datetime.fromisoformat(rec.last_heartbeat)
                age = (datetime.now(timezone.utc) - hb_time).total_seconds()
                if age > HEARTBEAT_STALE_SECONDS:
                    return False  # PID alive but heartbeat stale — wedged
            except (ValueError, TypeError):
                pass
        return True

    def _os_lock_acquire(self) -> Optional[int]:
        """Acquire an OS-level exclusive file lock (Windows + Unix).

        Returns a file descriptor on success, None on failure.
        """
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except (OSError, IOError):
            try:
                os.close(fd)
            except Exception:
                pass
            return None

    def _os_lock_release(self, fd: int) -> None:
        """Release the OS-level file lock."""
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass

    def _load(self) -> Optional[SessionRecord]:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text())
            return SessionRecord(**{k: v for k, v in data.items()
                                    if k in SessionRecord.__dataclass_fields__})
        except Exception:
            return None

    def _save(self) -> None:
        if self._record:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._record.to_dict(), indent=2))


# ===================================================================
# CLI: session status / force-release
# ===================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Session registry management")
    parser.add_argument("--status", action="store_true", help="Show current session status")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--force-release", action="store_true",
                        help="Force-release a stale/stuck session")
    args = parser.parse_args()

    reg = SessionRegistry()

    if args.force_release:
        rec = reg._load()
        if rec and rec.is_active():
            if reg._pid_alive(rec.pid):
                print(f"WARNING: PID {rec.pid} is still alive. Kill it first.")
                print(f"  taskkill /F /PID {rec.pid}")
            else:
                reg._record = rec
                reg.release(SessionPhase.FAILED)
                print(f"Force-released stale session: {rec.mode} (PID {rec.pid})")
        else:
            print("No active session to release.")
    elif args.json:
        current = reg.get_current()
        print(json.dumps(current or {"session": None}, indent=2, default=str))
    else:
        print(reg.get_status_display())


if __name__ == "__main__":
    main()
