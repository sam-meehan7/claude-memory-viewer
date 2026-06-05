# Claude Memory Viewer

A tiny, **fully-local** web app to browse, edit, and delete the memory files that
[Claude Code](https://claude.com/claude-code) stores under `~/.claude/projects/*/memory/`.

- 🔒 **Local only** — binds to `127.0.0.1`, no internet, nothing leaves your machine.
- 🪶 **Zero dependencies** — pure Python standard library. No `pip install`.
- 📝 View rendered markdown, edit the raw file, or delete a memory — with safety rails.
- 🗂 Groups memories by project, showing the real repo name for each.

## Requirements

- [uv](https://docs.astral.sh/uv/) (recommended), or any Python 3.8+.
- No third-party dependencies — the app is pure standard library.

## Quick start

With [uv](https://docs.astral.sh/uv/) (it manages the Python version and venv for you):

```bash
git clone https://github.com/<you>/claude-memory-viewer.git
cd claude-memory-viewer
uv run claude-memory-viewer
```

It starts on <http://127.0.0.1:8788> and opens your browser automatically.
Press `Ctrl+C` to stop.

> No uv? Since there are zero dependencies you can also just run
> `python3 server.py` with any Python 3.8+.

### Options

```
uv run claude-memory-viewer --port 9000      # use a different port
uv run claude-memory-viewer --no-browser     # don't auto-open the browser
uv run claude-memory-viewer --host 0.0.0.0   # expose on your LAN (see security note)
uv run claude-memory-viewer --root ~/other/.claude/projects   # custom projects dir
```

You can also point it elsewhere with an env var:

```bash
CLAUDE_PROJECTS_ROOT=/path/to/.claude/projects uv run claude-memory-viewer
```

## What it shows

Claude Code keeps per-project memory in folders like
`~/.claude/projects/-Users-you-Repos-my-service/memory/`. Each `*.md` file is one
memory with YAML-ish frontmatter (`name`, `description`, `type`) plus a markdown body;
a `MEMORY.md` in each folder is the index Claude loads each session.

The viewer:

- **Sidebar** — groups files by project. The encoded folder name is decoded back to a
  readable repo name (everything after the `Repos`/`repos` path segment).
- **Type badges** — `user` / `feedback` / `project` / `reference` / `index`.
- **View** — renders the markdown body (headings, lists, code, `**bold**`,
  `[[wikilinks]]`) with the frontmatter in a separate card.
- **Edit** — a raw editor over the whole file; **Save** writes straight to disk.
- **Delete** — with a confirmation dialog.
- **Filter box** — searches titles, descriptions, types, and project names.

## Safety

- File operations are restricted to `.md` files located **directly inside** a
  `*/memory` directory — anything outside is rejected.
- `MEMORY.md` index files are protected from deletion.
- The server binds to `127.0.0.1` by default. Only pass `--host 0.0.0.0` if you
  understand you're exposing read/write access to your memory files on your network.

## How it works

Everything is in a single file, [`server.py`](server.py): a `ThreadingHTTPServer`
exposing a small JSON API (`/api/memories`, `/api/memory`) and serving the UI, which
is one embedded HTML document with no external assets. `pyproject.toml` wires it up as
the `claude-memory-viewer` console script for `uv run`.
