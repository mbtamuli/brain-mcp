## Quick start

```bash
claude mcp add --scope user brain \
  --command uvx \
  --args "--from git+https://github.com/mbtamuli/brain-mcp brain serve" \
  --env BRAIN_DIR=~/brain
```

Requires a brain data directory at `BRAIN_DIR` (default `~/brain`) with `MEMORY.md` and `USER.md`.
Use [claude-brain-starter](https://github.com/mbtamuli/claude-brain-starter) to set one up.
