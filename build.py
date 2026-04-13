"""
Build SQLite + FTS5 index from ~/brain/MEMORY.md and ~/brain/USER.md.
stdlib only: sqlite3, pathlib
"""
import os
import sqlite3
import sys
from pathlib import Path

BRAIN_DIR = Path(os.environ.get("BRAIN_DIR", str(Path.home() / "brain")))
MEMORY_FILE = BRAIN_DIR / "MEMORY.md"
USER_FILE = BRAIN_DIR / "USER.md"
INDEX_DIR = BRAIN_DIR / ".index"
DB_PATH = INDEX_DIR / "brain.db"

ENTRY_DELIMITER = "\n§\n"


def parse_entries(file_path: Path) -> list:
    """Parse §-delimited entries from a memory file."""
    if not file_path.exists():
        return []
    try:
        raw = file_path.read_text(encoding="utf-8")
    except (OSError, IOError):
        return []
    if not raw.strip():
        return []
    entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
    return [e for e in entries if e]


def build_index(full: bool = False) -> dict:
    """Build or rebuild the FTS5 index from MEMORY.md and USER.md.

    Args:
        full: If True, drop and recreate the table. If False, clear and re-insert
              (same result, but preserves table schema for in-place rebuild).

    Returns:
        dict with keys: sources (dict of source->count), total (int), db (str), full (bool)
    """
    INDEX_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    try:
        cursor = conn.cursor()

        if full:
            cursor.execute("DROP TABLE IF EXISTS brain_entries")

        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS brain_entries USING fts5(
                content,
                source UNINDEXED,
                position UNINDEXED,
                tokenize = 'unicode61'
            )
        """)

        # Clear existing entries to avoid duplicates on re-run
        cursor.execute("DELETE FROM brain_entries")

        stores = [
            (MEMORY_FILE, "memory"),
            (USER_FILE, "user"),
        ]

        topics_dir = BRAIN_DIR / "topics"
        if topics_dir.is_dir():
            for topic_file in sorted(topics_dir.glob("*.md")):
                stores.append((topic_file, f"topic:{topic_file.stem}"))

        sources = {}
        total = 0
        for file_path, source in stores:
            entries = parse_entries(file_path)
            for i, entry in enumerate(entries):
                cursor.execute(
                    "INSERT INTO brain_entries(content, source, position) VALUES (?, ?, ?)",
                    (entry, source, i),
                )
            sources[source] = len(entries)
            total += len(entries)

        conn.commit()
    finally:
        conn.close()

    return {"sources": sources, "total": total, "db": str(DB_PATH), "full": full}


def update_entry(source: str, entries: list) -> None:
    """Update the FTS5 index for a single source after a brain_write.

    Called by write.py after every successful write. Markdown file is
    the source of truth; this keeps brain.db current. If DB update fails,
    the file write already succeeded -- run 'brain build' to restore consistency.

    Args:
        source: 'memory' or 'user'
        entries: Full list of current entries for this source
    """
    if not DB_PATH.exists():
        # Index not built yet; silently skip
        return

    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='brain_entries'"
            )
            if not cursor.fetchone():
                return
            cursor.execute("DELETE FROM brain_entries WHERE source = ?", (source,))
            for i, entry in enumerate(entries):
                cursor.execute(
                    "INSERT INTO brain_entries(content, source, position) VALUES (?, ?, ?)",
                    (entry, source, i),
                )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as e:
        print(
            f"Warning: brain.db update failed after write: {e}. "
            "Run 'brain build' to restore consistency.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build brain FTS5 index")
    parser.add_argument("--full", action="store_true", help="Force full rebuild (drop + recreate)")
    args = parser.parse_args()
    build_index(full=args.full)
