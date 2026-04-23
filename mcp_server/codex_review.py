"""
Codex Review Automation
=======================
Packages changed files + context and runs Codex CLI for review.
Works both with and without git (uses file diffing fallback).

Usage:
    python -m mcp_server.codex_review --files file1.py file2.py
    python -m mcp_server.codex_review --uncommitted          # git mode
    python -m mcp_server.codex_review --files file1.py --context "Added MCP tool for X"
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

QB_ROOT = Path(__file__).resolve().parents[1]

# Find Codex CLI: prefer PATH, then VS Code extension
_CODEX_EXT_DIR = Path("C:/Users/Sunda/.vscode/extensions")
_CODEX_CANDIDATES = sorted(
    _CODEX_EXT_DIR.glob("openai.chatgpt-*/bin/windows-x86_64/codex.exe"),
    reverse=True,
)


def _find_codex() -> Path:
    """Find Codex CLI: check PATH first, then VS Code extension."""
    path_codex = shutil.which("codex")
    if path_codex:
        return Path(path_codex)
    if _CODEX_CANDIDATES:
        return _CODEX_CANDIDATES[0]
    return Path("codex.exe")  # will fail gracefully


CODEX_EXE = _find_codex()
REVIEW_OUTPUT_DIR = QB_ROOT / "data" / "reviews"
REVIEW_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _is_git_repo() -> bool:
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(QB_ROOT), capture_output=True, check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _file_contents(files: list[str]) -> str:
    """Read file contents for review."""
    output = []
    for f in files:
        fpath = Path(f) if Path(f).is_absolute() else QB_ROOT / f
        if fpath.exists():
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
                rel = fpath.relative_to(QB_ROOT) if str(fpath).startswith(str(QB_ROOT)) else fpath
                output.append(f"=== {rel} ===\n{content}")
            except Exception as e:
                output.append(f"=== {f} === ERROR: {e}")
        else:
            output.append(f"=== {f} === FILE NOT FOUND")
    return "\n\n".join(output)


def run_codex_review(
    files: list[str] | None = None,
    uncommitted: bool = False,
    context: str = "",
    title: str = "",
) -> dict:
    """Run Codex review and return structured result."""
    if not CODEX_EXE.exists():
        return {"status": "error", "message": f"Codex CLI not found at {CODEX_EXE}"}

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = REVIEW_OUTPUT_DIR / f"review_{timestamp}.md"

    # Build review prompt pieces
    prompt_parts = []
    if title:
        prompt_parts.append(f"## Change Title\n{title}")
    if context:
        prompt_parts.append(f"## Change Context\n{context}")

    # Determine mode
    use_git = _is_git_repo() and uncommitted

    if use_git:
        # codex review --uncommitted (no --skip-git-repo-check, it's not supported)
        cmd = [str(CODEX_EXE), "review", "--uncommitted"]
        if title:
            cmd.extend(["--title", title])
        if prompt_parts:
            cmd.append("\n\n".join(prompt_parts))

        try:
            result = subprocess.run(
                cmd, cwd=str(QB_ROOT),
                capture_output=True, text=True, timeout=300,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "message": "Codex review timed out after 5 minutes"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

        if result.returncode != 0 and not result.stdout.strip():
            return {
                "status": "error",
                "message": f"Codex exited {result.returncode}: {result.stderr[:1000]}",
                "returncode": result.returncode,
            }
        review_text = result.stdout

    else:
        # Non-git: build full prompt with file contents, write to temp file
        if files:
            file_content = _file_contents(files)
            prompt_parts.append(f"## Files to Review\n{file_content}")

        review_prompt = (
            "You are reviewing code for the Quant-Bridge project. "
            "Read the project instructions in .codex/instructions.md for the full review checklist. "
            "Review the following code changes and provide a structured review "
            "with VALIDATED or CHANGES REQUESTED status.\n\n"
            + "\n\n".join(prompt_parts)
        )

        # Write full prompt to temp file to avoid truncation or encoding issues
        prompt_file = REVIEW_OUTPUT_DIR / f"prompt_{timestamp}.txt"
        prompt_file.write_text(review_prompt, encoding="utf-8")

        # Read from temp file via shell: `type prompt.txt | codex exec ...`
        # Or pass directly if short enough; otherwise use stdin from file
        cmd = [
            str(CODEX_EXE), "exec",
            "--full-auto",
            "-C", str(QB_ROOT),
            "--skip-git-repo-check",
            "-o", str(output_file),
        ]

        try:
            # Feed prompt via stdin from the file
            with open(prompt_file, "r", encoding="utf-8") as pf:
                result = subprocess.run(
                    cmd, cwd=str(QB_ROOT),
                    stdin=pf,
                    capture_output=True, text=True, timeout=300,
                    encoding="utf-8", errors="replace",
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                )
        except subprocess.TimeoutExpired:
            prompt_file.unlink(missing_ok=True)
            return {"status": "timeout", "message": "Codex review timed out after 5 minutes"}
        except Exception as e:
            prompt_file.unlink(missing_ok=True)
            return {"status": "error", "message": str(e)}
        finally:
            prompt_file.unlink(missing_ok=True)

        if result.returncode != 0 and not result.stdout.strip():
            # Check if output file was written despite non-zero exit
            if output_file.exists() and output_file.stat().st_size > 0:
                review_text = output_file.read_text(encoding="utf-8", errors="replace")
            else:
                return {
                    "status": "error",
                    "message": f"Codex exited {result.returncode}: {result.stderr[:1000]}",
                    "returncode": result.returncode,
                }
        else:
            # Prefer output file if written, else stdout
            if output_file.exists() and output_file.stat().st_size > 0:
                review_text = output_file.read_text(encoding="utf-8", errors="replace")
            else:
                review_text = result.stdout

    # Save final review with metadata header
    output_file.write_text(
        f"# Codex Review - {timestamp}\n"
        f"**Title:** {title or 'Untitled'}\n"
        f"**Context:** {context[:200] or 'None'}\n"
        f"**Files:** {', '.join(files or ['uncommitted'])}\n\n"
        f"---\n\n{review_text}",
        encoding="utf-8",
    )

    return {
        "status": "completed",
        "review_text": review_text[:5000],
        "output_file": str(output_file),
        "timestamp": timestamp,
        "returncode": result.returncode,
    }


def check_auth() -> dict:
    """Check if Codex CLI is authenticated."""
    try:
        result = subprocess.run(
            [str(CODEX_EXE), "login", "status"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            return {"authenticated": True, "output": result.stdout.strip()}
        return {"authenticated": False, "output": result.stderr.strip()}
    except Exception as e:
        return {"authenticated": False, "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Run Codex code review")
    parser.add_argument("--files", nargs="+", help="Files to review")
    parser.add_argument("--uncommitted", action="store_true", help="Review all uncommitted changes")
    parser.add_argument("--context", default="", help="Description of the changes")
    parser.add_argument("--title", default="", help="Title for the review")
    parser.add_argument("--check-auth", action="store_true", help="Check Codex authentication")
    args = parser.parse_args()

    if args.check_auth:
        print(json.dumps(check_auth(), indent=2))
        return

    result = run_codex_review(
        files=args.files,
        uncommitted=args.uncommitted,
        context=args.context,
        title=args.title,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
