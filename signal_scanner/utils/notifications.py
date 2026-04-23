"""Desktop notification system.

Only notifies for NEW high-conviction signals that haven't been
seen recently. Logs all alerts to console regardless.
"""

from typing import Dict, Set, Tuple

from loguru import logger

# Track recently notified signals to avoid spamming
# Key: (symbol, signal_direction), cleared each scan cycle via reset()
_notified: Set[Tuple[str, str]] = set()
_enabled: bool = False  # Disabled by default — enable with --alerts flag


def enable() -> None:
    """Enable desktop notifications."""
    global _enabled
    _enabled = True


def reset() -> None:
    """Clear the notification cache. Call at the start of each scan cycle."""
    _notified.clear()


def send_notification(title: str, message: str, symbol: str = "", signal: str = "") -> None:
    """Send a desktop notification if the signal is new.

    Args:
        title: Notification title.
        message: Notification body text.
        symbol: Ticker symbol (for dedup).
        signal: Signal direction (for dedup).
    """
    # Always log to console
    logger.info(f"ALERT: {title} — {message}")

    # Deduplicate: skip if we already notified this symbol+direction this cycle
    key = (symbol, signal)
    if key in _notified:
        return
    _notified.add(key)

    if not _enabled:
        return

    _send_windows_toast(title, message)


def _send_windows_toast(title: str, message: str) -> None:
    """Send a Windows balloon notification. Non-blocking, short-lived."""
    import sys
    if sys.platform != "win32":
        return

    try:
        import subprocess
        # Short 3-second display, then dispose immediately — no lingering
        ps_script = (
            'Add-Type -AssemblyName System.Windows.Forms;'
            '$n = New-Object System.Windows.Forms.NotifyIcon;'
            '$n.Icon = [System.Drawing.SystemIcons]::Information;'
            '$n.Visible = $true;'
            f'$n.ShowBalloonTip(3000, "{_esc(title)}", "{_esc(message)}", '
            '[System.Windows.Forms.ToolTipIcon]::Info);'
            'Start-Sleep -Seconds 3;'
            '$n.Visible = $false;'
            '$n.Dispose()'
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        logger.debug(f"Toast notification failed: {e}")


def _esc(text: str) -> str:
    """Escape text for PowerShell string literals."""
    return text.replace('"', '`"').replace("'", "`'").replace("\n", " ")
