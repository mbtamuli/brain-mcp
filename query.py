"""
brain_search implementation -- FTS5 keyword search over brain entries.

Returns clean formatted results with source attribution. No LLM call.
Results are meant to appear as tool output in conversation context,
not injected into the system prompt.
"""
import os
import re
import sqlite3
from pathlib import Path

BRAIN_DIR = Path(os.environ.get("BRAIN_DIR", str(Path.home() / "brain")))
DB_PATH = BRAIN_DIR / ".index" / "brain.db"


def _preprocess_query(query: str) -> str:
    """Replace word-internal hyphens with spaces to avoid FTS5 NOT misparse.

    FTS5 treats '-' as a NOT operator. A word-internal hyphen like 'cc-control'
    would be parsed as 'cc NOT control', causing a hard error (NOT at start).
    Replacing with a space produces 'cc control' (AND), which is close enough.
    Intentional leading '-' for NOT exclusion (e.g. 'toolchain -deprecated')
    is preserved because it is not word-internal.
    """
    return re.sub(r"(?<=\S)-(?=\S)", " ", query)


def query_brain(query: str, limit: int = 10) -> str:
    """Search brain entries via FTS5. Returns formatted string for tool output.

    Supports FTS5 query syntax: AND (default), OR, phrase quotes,
    prefix wildcard (*), NOT.

    Args:
        query: FTS5 query string
        limit: Maximum number of results

    Returns:
        Formatted string with results grouped by source store.
        Never raises -- errors returned as formatted error strings.
    """
    if not DB_PATH.exists():
        return (
            f"Brain index not found. Run: brain build\n"
            f"(Expected at: {DB_PATH})"
        )

    q = _preprocess_query(query.strip())
    if not q:
        return "Query cannot be empty."

    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='brain_entries'"
            )
            if not cursor.fetchone():
                return "Brain index is empty or corrupted. Run: brain build"

            cursor.execute(
                """
                SELECT content, source, rank
                FROM brain_entries
                WHERE brain_entries MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (q, limit),
            )
            rows = cursor.fetchall()
        finally:
            conn.close()

    except sqlite3.OperationalError as e:
        # Likely an FTS5 query syntax error
        return (
            f"Search error: {e}\n"
            "Tip: FTS5 supports AND (default), OR, NOT, \"phrase\", prefix*"
        )
    except sqlite3.Error as e:
        return f"Database error: {e}"

    if not rows:
        return f"No results for: {query}"

    memory_results = [(content, rank) for content, source, rank in rows if source == "memory"]
    user_results = [(content, rank) for content, source, rank in rows if source == "user"]
    topic_results = [(content, source, rank) for content, source, rank in rows if source.startswith("topic:")]

    lines = [f"Search results for: {query}\n"]

    if memory_results:
        lines.append("--- MEMORY ---")
        for content, _ in memory_results:
            clean = content.replace("\u00a7", "").strip()
            lines.append(f"{clean}\n[source: memory]")
            lines.append("")

    if user_results:
        lines.append("--- USER PROFILE ---")
        for content, _ in user_results:
            clean = content.replace("\u00a7", "").strip()
            lines.append(f"{clean}\n[source: user]")
            lines.append("")

    if topic_results:
        lines.append("--- TOPICS ---")
        for content, source, _ in topic_results:
            clean = content.replace("\u00a7", "").strip()
            lines.append(f"{clean}\n[source: {source}]")
            lines.append("")

    return "\n".join(lines).rstrip()
