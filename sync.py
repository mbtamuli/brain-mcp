"""
Event-driven git sync for the brain data repo.

Runs git pull --rebase && git push in the background after tool calls,
rate-limited by a .last_sync timestamp file (15-minute cooldown).
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

SYNC_COOLDOWN = timedelta(minutes=15)


def should_sync(brain_dir: Path) -> bool:
    """Return True if .last_sync is missing or older than 15 minutes."""
    last_sync_file = Path(brain_dir) / ".last_sync"
    if not last_sync_file.exists():
        return True
    try:
        ts = datetime.fromisoformat(last_sync_file.read_text().strip())
        return datetime.now(timezone.utc) - ts > SYNC_COOLDOWN
    except (ValueError, OSError):
        return True


def mark_synced(brain_dir: Path) -> None:
    """Write current ISO timestamp to .last_sync."""
    last_sync_file = Path(brain_dir) / ".last_sync"
    last_sync_file.write_text(datetime.now(timezone.utc).isoformat())


async def run_sync(brain_dir: Path) -> None:
    """Run git pull --rebase && git push if cooldown has expired.

    Runs as a background task. Logs errors to stderr, never raises.
    Does NOT update .last_sync on failure.
    """
    try:
        brain_dir = Path(brain_dir)
        if not should_sync(brain_dir):
            return

        # git pull --rebase
        pull = await asyncio.create_subprocess_exec(
            "git", "-C", str(brain_dir), "pull", "--rebase",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        pull_stdout, pull_stderr = await pull.communicate()

        if pull.returncode != 0:
            logger.error(
                "brain sync: git pull --rebase failed (rc=%d): %s",
                pull.returncode,
                pull_stderr.decode().strip(),
            )
            # Abort the failed rebase
            abort = await asyncio.create_subprocess_exec(
                "git", "-C", str(brain_dir), "rebase", "--abort",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await abort.communicate()
            return

        # git push
        push = await asyncio.create_subprocess_exec(
            "git", "-C", str(brain_dir), "push",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        push_stdout, push_stderr = await push.communicate()

        if push.returncode != 0:
            logger.error(
                "brain sync: git push failed (rc=%d): %s",
                push.returncode,
                push_stderr.decode().strip(),
            )
            return

        mark_synced(brain_dir)
        logger.info("brain sync: completed successfully")

    except Exception:
        logger.exception("brain sync: unexpected error")
