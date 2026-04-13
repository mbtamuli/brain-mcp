"""
brain_write implementation -- write entries to MEMORY.md or USER.md.

Derived from Hermes memory_tool.py (NousResearch/hermes-agent).
"""
import fcntl
import json
import logging
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BRAIN_DIR = Path(os.environ.get("BRAIN_DIR", str(Path.home() / "brain")))
ENTRY_DELIMITER = "\n§\n"

# Character limits
MEMORY_CHAR_LIMIT = 2200
USER_CHAR_LIMIT = 1375

# ---------------------------------------------------------------------------
# Security scan
# ---------------------------------------------------------------------------

_MEMORY_THREAT_PATTERNS = [
    (r"ignore\s+(previous|all|above|prior)\s+instructions", "prompt_injection"),
    (r"you\s+are\s+now\s+", "role_hijack"),
    (r"do\s+not\s+tell\s+the\s+user", "deception_hide"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    (r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)", "disregard_rules"),
    (
        r"act\s+as\s+(if|though)\s+you\s+(have\s+no|don't\s+have)\s+(restrictions|limits|rules)",
        "bypass_restrictions",
    ),
    (r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl"),
    (r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_wget"),
    (r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)", "read_secrets"),
    (r"authorized_keys", "ssh_backdoor"),
    (r"\$HOME/\.ssh|\~/\.ssh", "ssh_access"),
]

_INVISIBLE_CHARS = {
    "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff",
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
}


def _scan_memory_content(content: str) -> Optional[str]:
    """Scan memory content for injection/exfil patterns. Returns error string if blocked."""
    for char in _INVISIBLE_CHARS:
        if char in content:
            return (
                f"Blocked: content contains invisible unicode character "
                f"U+{ord(char):04X} (possible injection)."
            )
    for pattern, pid in _MEMORY_THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return (
                f"Blocked: content matches threat pattern '{pid}'. "
                "Brain entries are injected into session context and must not "
                "contain injection or exfiltration payloads."
            )
    return None


# ---------------------------------------------------------------------------
# BrainStore
# ---------------------------------------------------------------------------


class BrainStore:
    """Bounded curated brain store for MEMORY.md and USER.md."""

    def __init__(
        self,
        memory_char_limit: int = MEMORY_CHAR_LIMIT,
        user_char_limit: int = USER_CHAR_LIMIT,
    ):
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit

    # -- File paths --

    @staticmethod
    def _path_for(target: str) -> Path:
        if target == "user":
            return BRAIN_DIR / "USER.md"
        if target == "memory":
            return BRAIN_DIR / "MEMORY.md"
        if target.startswith("topic:"):
            stem = target[6:]
            if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9-]*$', stem):
                raise ValueError(
                    f"Invalid topic stem '{stem}'. "
                    "Stems must be alphanumeric and hyphens only (kebab-case), "
                    "e.g. 'topic:toolchain' or 'topic:agent-architecture'."
                )
            return BRAIN_DIR / "topics" / f"{stem}.md"
        raise ValueError(
            f"Unknown target '{target}'. "
            "Valid targets: 'memory', 'user', or 'topic:<name>' (e.g. 'topic:toolchain')."
        )

    # -- File I/O --

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """Read a brain file and split into entries.

        No file locking needed here: _write_file uses atomic rename, so readers
        always see either the previous complete file or the new one.
        """
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return []
        if not raw.strip():
            return []
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    @staticmethod
    def _write_file(path: Path, entries: List[str]) -> None:
        """Write entries via atomic tempfile + os.replace.

        Atomic rename avoids the race window where open("w") truncates before
        the lock is acquired. Readers always see a complete file.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix=".tmp", prefix=".brain_"
        )
        try:
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(path))
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (OSError, IOError) as e:
            raise RuntimeError(f"Failed to write {path}: {e}")

    # -- File locking --

    @staticmethod
    @contextmanager
    def _file_lock(path: Path):
        """Acquire exclusive lock via .lock sidecar file.

        Uses a separate .lock file so the memory file itself can still be
        atomically replaced via os.replace().
        """
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(lock_path, "w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()

    # -- Helpers --

    def _char_limit(self, target: str) -> Optional[int]:
        if target.startswith("topic:"):
            return None
        return self.user_char_limit if target == "user" else self.memory_char_limit

    @staticmethod
    def _char_count(entries: List[str]) -> int:
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _reload(self, target: str) -> List[str]:
        """Re-read entries from disk under file lock."""
        entries = self._read_file(self._path_for(target))
        return list(dict.fromkeys(entries))  # deduplicate, preserve order

    def _update_index(self, target: str, entries: List[str]) -> None:
        """Update brain.db after a successful write. Non-fatal if DB unavailable."""
        try:
            from build import update_entry

            update_entry(target, entries)
        except Exception as e:
            logger.warning(
                "brain.db update failed: %s. Run 'brain build' to restore consistency.", e
            )

    def _success_response(
        self, target: str, entries: List[str], message: str = None
    ) -> Dict[str, Any]:
        """Build success response with usage stats."""
        current = self._char_count(entries)
        limit = self._char_limit(target)
        resp: Dict[str, Any] = {
            "success": True,
            "target": target,
            "entries": entries,
            "entry_count": len(entries),
        }
        if limit is None:
            resp["usage"] = f"{current:,} chars, {len(entries)} entries"
        else:
            pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
            resp["usage"] = f"{pct}% \u2014 {current:,}/{limit:,} chars"
        if message:
            resp["message"] = message
        return resp

    # -- Actions --

    def add(self, target: str, content: str, force: bool = False) -> Dict[str, Any]:
        """Append a new entry to the target store.

        Args:
            target: 'memory', 'user', or 'topic:<name>'
            content: Entry text
            force: If True, bypass size limit and security scan (not duplicate check)
        """
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        try:
            path = self._path_for(target)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if not force:
            scan_error = _scan_memory_content(content)
            if scan_error:
                return {"success": False, "error": scan_error}

        with self._file_lock(path):
            file_existed = path.exists()
            entries = self._reload(target)
            limit = self._char_limit(target)

            # Reject exact duplicates (force does NOT bypass this)
            if content in entries:
                return self._success_response(
                    target, entries, "Entry already exists (no duplicate added)."
                )

            if not force and limit is not None:
                new_entries = entries + [content]
                new_total = self._char_count(new_entries)
                if new_total > limit:
                    current = self._char_count(entries)
                    return {
                        "success": False,
                        "error": (
                            f"Memory at {current:,}/{limit:,} chars. "
                            f"Adding this entry ({len(content)} chars) would exceed the limit. "
                            "Replace or remove existing entries first."
                        ),
                        "current_entries": entries,
                        "usage": f"{current:,}/{limit:,}",
                    }

            entries.append(content)
            self._write_file(path, entries)

        self._update_index(target, entries)
        msg = "Entry added." + (" (force=true)" if force else "")
        resp = self._success_response(target, entries, msg)
        if not file_existed and target.startswith("topic:"):
            topics_dir = BRAIN_DIR / "topics"
            resp["existing_topics"] = sorted(p.stem for p in topics_dir.glob("*.md"))
        return resp

    def replace(
        self, target: str, old_text: str, new_content: str, force: bool = False
    ) -> Dict[str, Any]:
        """Replace an entry identified by unique substring.

        Args:
            target: 'memory', 'user', or 'topic:<name>'
            old_text: Unique substring identifying the entry to replace
            new_content: Replacement entry text
            force: If True, bypass size limit and security scan
        """
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {
                "success": False,
                "error": "new_content cannot be empty. Use 'remove' to delete entries.",
            }

        try:
            path = self._path_for(target)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if not force:
            scan_error = _scan_memory_content(new_content)
            if scan_error:
                return {"success": False, "error": scan_error}

        with self._file_lock(path):
            entries = self._reload(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                unique_texts = set(e for _, e in matches)
                if len(unique_texts) > 1:
                    previews = [
                        e[:80] + ("..." if len(e) > 80 else "") for _, e in matches
                    ]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # All identical -- operate on first

            idx = matches[0][0]
            limit = self._char_limit(target)

            if not force and limit is not None:
                test_entries = entries.copy()
                test_entries[idx] = new_content
                if self._char_count(test_entries) > limit:
                    return {
                        "success": False,
                        "error": (
                            f"Replacement would exceed {limit:,} char limit. "
                            "Shorten the new content or remove other entries first."
                        ),
                    }

            entries[idx] = new_content
            self._write_file(path, entries)

        self._update_index(target, entries)
        return self._success_response(target, entries, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        """Remove an entry identified by unique substring.

        Args:
            target: 'memory', 'user', or 'topic:<name>'
            old_text: Unique substring identifying the entry to remove
        """
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        try:
            path = self._path_for(target)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        with self._file_lock(path):
            entries = self._reload(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                unique_texts = set(e for _, e in matches)
                if len(unique_texts) > 1:
                    previews = [
                        e[:80] + ("..." if len(e) > 80 else "") for _, e in matches
                    ]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }

            idx = matches[0][0]
            entries.pop(idx)
            self._write_file(path, entries)

        self._update_index(target, entries)
        return self._success_response(target, entries, "Entry removed.")

    def read(self, target: str) -> List[str]:
        """Read current entries from the target store."""
        return self._read_file(self._path_for(target))

    def render_block(self, target: str, entries: List[str]) -> str:
        """Render a snapshot block with header and usage indicator.

        Used by the SessionStart hook to produce the frozen snapshot.
        """
        if not entries:
            return ""
        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "user":
            header = f"USER PROFILE (who the user is) [{pct}% \u2014 {current:,}/{limit:,} chars]"
        else:
            header = f"MEMORY (your personal notes) [{pct}% \u2014 {current:,}/{limit:,} chars]"

        separator = "\u2550" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"
