"""
Brain MCP server -- exposes brain_write and brain_search as MCP tools.

Uses FastMCP from the mcp package.

Usage:
    brain serve
    uvx --from git+https://github.com/mbtamuli/brain-mcp brain serve
"""
import json
import logging
import sys

logger = logging.getLogger(__name__)

BRAIN_GUIDANCE = (
    "You have persistent memory across sessions via brain_write and brain_search tools. "
    "Save durable facts using brain_write: user preferences, environment details, tool quirks, "
    "and stable conventions. Memory is injected into every session start, so keep it compact "
    "and focused on facts that will still matter later.\n"
    "Prioritize what reduces future user steering -- the most valuable memory is one that "
    "prevents the user from having to correct or remind you again. "
    "User preferences and recurring corrections matter more than procedural task details.\n"
    "Do NOT save task progress, session outcomes, completed-work logs, or temporary TODO "
    "state to brain; these belong in session history.\n"
    "When the user references something from a past conversation or you suspect relevant "
    "cross-session context exists, use brain_search to recall it before asking them to "
    "repeat themselves."
)

BRAIN_WRITE_DESCRIPTION = (
    "Save durable information to persistent brain memory that survives across sessions. "
    "Brain content is injected into future session starts, so keep entries compact and "
    "focused on facts that will still matter later.\n\n"
    "WHEN TO SAVE (do this proactively, don't wait to be asked):\n"
    "- User corrects you or says 'remember this' / 'don't do that again'\n"
    "- User shares a preference, habit, or personal detail (name, role, timezone, coding style)\n"
    "- You discover something about the environment (OS, installed tools, project structure)\n"
    "- You learn a convention, API quirk, or workflow specific to this user's setup\n"
    "- You identify a stable fact that will be useful again in future sessions\n\n"
    "PRIORITY: User preferences and corrections > environment facts > procedural knowledge. "
    "The most valuable memory prevents the user from having to repeat themselves.\n\n"
    "Do NOT save task progress, session outcomes, completed-work logs, or temporary TODO "
    "state; these belong in session history.\n\n"
    "THREE TARGETS:\n"
    "- 'memory': your notes -- environment facts, project conventions, tool quirks, lessons learned\n"
    "- 'user': who the user is -- name, role, preferences, communication style, pet peeves\n"
    "- 'topic:<name>': domain-organized reference content (e.g. 'topic:toolchain', "
    "'topic:agent-architecture'). Searchable on demand via brain_search. Never injected at session "
    "start, so no char limit applies. Auto-creates the file if it does not exist. Use for stable "
    "reference material that is too large or too domain-specific for MEMORY.md. "
    "Prefer multiple short §-separated entries over a single large document.\n\n"
    "ACTIONS: add (new entry), replace (update existing: old_text locates the entry by matching "
    "a substring within it, content replaces the whole matched entry — not a substring swap), "
    "remove (delete via old_text substring).\n\n"
    "SKIP: trivial/obvious info, things easily re-discovered, raw data dumps, temporary task state."
)

BRAIN_SEARCH_DESCRIPTION = (
    "Search brain memory for relevant entries using FTS5 keyword search. "
    "Returns entries from MEMORY.md, USER.md, and topic files (~/brain/topics/*.md) "
    "ranked by relevance. Results are grouped into three sections: MEMORY, USER PROFILE, "
    "and TOPICS, each with source attribution.\n\n"
    "WHEN TO SEARCH:\n"
    "- User references something from a past session without restating it\n"
    "- You suspect relevant prior context exists but it's not in the current session snapshot\n"
    "- After brain_write adds an entry mid-session (snapshot won't refresh until next session)\n\n"
    "QUERY SYNTAX (FTS5):\n"
    "- Default: AND (all terms must appear)\n"
    "- OR: 'logging OR monitoring'\n"
    "- Phrase: '\"exact phrase\"'\n"
    "- Prefix: 'observ*' matches observe, observability, etc.\n"
    "- NOT: 'logging NOT docker'\n\n"
    "SEARCH STRATEGY:\n"
    "- Use single topic-name terms for discovery (e.g. 'session-peers', not 'brain MCP session-peers')\n"
    "- Avoid querying by tool names -- tool names are not stored as text in any entry\n"
    "- On no results: split the query and try each term separately\n"
    "- Topic names are single words or hyphenated (e.g. 'session-peers', 'toolchain')\n\n"
    "Results appear as tool output in the conversation, not in the system prompt. "
    "Search before asking the user to repeat or re-explain past context."
)


def run_mcp_server(verbose: bool = False) -> None:
    """Start the brain MCP server on stdio."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print(
            "Error: MCP server requires the 'mcp' package.\n"
            "Install via: uvx --from git+https://github.com/mbtamuli/brain-mcp brain serve",
            file=sys.stderr,
        )
        sys.exit(1)

    if verbose:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
    else:
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    import sys as _sys
    from pathlib import Path as _Path
    _pkg_dir = _Path(__file__).parent
    if str(_pkg_dir) not in _sys.path:
        _sys.path.insert(0, str(_pkg_dir))

    import asyncio
    from write import BrainStore, BRAIN_DIR
    from query import query_brain
    from build import build_index
    from sync import run_sync

    mcp = FastMCP("brain", instructions=BRAIN_GUIDANCE)
    store = BrainStore()

    @mcp.tool(description=BRAIN_WRITE_DESCRIPTION)
    async def brain_write(
        action: str,
        content: str = None,
        target: str = "memory",
        old_text: str = None,
        force: bool = False,
    ) -> str:
        """Write to brain persistent memory.

        Args:
            action: 'add', 'replace', or 'remove'
            content: Entry content (required for add/replace)
            target: 'memory' (agent notes), 'user' (user profile), or 'topic:<name>'
                (domain reference, e.g. 'topic:toolchain'). Default: 'memory'
            old_text: Unique substring identifying the entry to replace/remove
            force: Bypass size limit and security scan (duplicate check still applies)
        """
        if action == "add":
            if not content:
                return json.dumps({"success": False, "error": "content is required for 'add'."})
            result = store.add(target, content, force=force)
        elif action == "replace":
            if not old_text:
                return json.dumps({"success": False, "error": "old_text is required for 'replace'."})
            if not content:
                return json.dumps({"success": False, "error": "content is required for 'replace'."})
            result = store.replace(target, old_text, content, force=force)
        elif action == "remove":
            if not old_text:
                return json.dumps({"success": False, "error": "old_text is required for 'remove'."})
            result = store.remove(target, old_text)
        else:
            result = {
                "success": False,
                "error": f"Unknown action '{action}'. Use: add, replace, remove",
            }
        asyncio.create_task(run_sync(BRAIN_DIR))
        return json.dumps(result, ensure_ascii=False, indent=2)

    BRAIN_REBUILD_DESCRIPTION = (
        "Maintenance/recovery operation: rebuild the FTS5 search index from source .md files "
        "(MEMORY.md, USER.md, and all topics/*.md). Use when brain_search returns no results "
        "or stale results after writes that bypassed brain_write, or after index corruption.\n\n"
        "Parameters:\n"
        "- full: If True, drops and recreates the FTS5 table before re-indexing (deep rebuild). "
        "Default False (in-place: clear + re-insert, faster and safe for normal use).\n\n"
        "Returns a JSON result with indexed entry counts per source and total."
    )

    @mcp.tool(description=BRAIN_REBUILD_DESCRIPTION)
    def brain_rebuild(full: bool = False) -> str:
        """Rebuild the FTS5 brain index from source .md files.

        Args:
            full: If True, drop and recreate the FTS5 table (deep rebuild).
                  Default False (in-place rebuild).
        """
        result = build_index(full=full)
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool(description=BRAIN_SEARCH_DESCRIPTION)
    async def brain_search(query: str, limit: int = 10) -> str:
        """Search brain memory via FTS5 keyword search.

        Args:
            query: FTS5 query string (AND/OR/NOT/phrase/prefix supported)
            limit: Maximum results to return (default 10)
        """
        result = query_brain(query, limit=limit)
        asyncio.create_task(run_sync(BRAIN_DIR))
        return result

    async def _run():
        await mcp.run_stdio_async()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run_mcp_server()
