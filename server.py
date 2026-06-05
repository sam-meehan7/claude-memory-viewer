#!/usr/bin/env python3
"""
Claude Memory Viewer
====================
A fully-local web app to view, edit, and delete the memory files Claude Code
stores under ~/.claude/projects/*/memory/.

No internet, no dependencies — Python standard library only.

Usage (uv-managed):
    uv run claude-memory-viewer                # serves on http://127.0.0.1:8788, opens a browser
    uv run claude-memory-viewer --port 9000    # custom port
    uv run claude-memory-viewer --no-browser   # don't auto-open a browser

Or directly with any Python 3.8+ (no dependencies to install):
    python3 server.py [--port ... --no-browser --root ...]
"""

import argparse
import json
import os
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECTS_ROOT = os.environ.get(
    "CLAUDE_PROJECTS_ROOT", os.path.expanduser("~/.claude/projects")
)


# --------------------------------------------------------------------------- #
# Filesystem helpers
# --------------------------------------------------------------------------- #
def memory_roots():
    """Return list of absolute paths to every */memory dir under projects."""
    roots = []
    if not os.path.isdir(PROJECTS_ROOT):
        return roots
    for entry in sorted(os.listdir(PROJECTS_ROOT)):
        mem = os.path.join(PROJECTS_ROOT, entry, "memory")
        if os.path.isdir(mem):
            roots.append(mem)
    return roots


def split_project(dirname):
    """
    Claude encodes the project path by replacing '/' with '-' and prefixing '-',
    which is lossy because real '-' inside a repo name look identical to separators.
    Heuristic: everything after the last 'Repos'/'repos' segment is the actual repo
    name (dashes preserved), since dirs live at ~/Repos/<repo-name>.

    e.g. '-Users-sam-Repos-dsch-dd-search-agg'
         -> label 'dsch-dd-search-agg', path '/Users/sam/Repos/dsch-dd-search-agg'

    Returns (label, display_path). The raw dirname stays the canonical key.
    """
    parts = [p for p in dirname.strip("-").split("-") if p != ""]
    repo_idx = None
    for i, p in enumerate(parts):
        if p.lower() == "repos":
            repo_idx = i  # take the last 'repos' segment
    if repo_idx is not None and repo_idx < len(parts) - 1:
        prefix = parts[: repo_idx + 1]
        repo = "-".join(parts[repo_idx + 1:])
        return repo, "/" + "/".join(prefix) + "/" + repo
    # No repo segment (or path ends at 'Repos'): fall back to slash-joined path.
    path = "/" + "/".join(parts)
    label = parts[-1] if parts else dirname
    home = os.path.expanduser("~")
    if path == home:
        label = "~ (home)"
    return label, path


def is_safe_path(path):
    """True only if `path` resolves to a .md file directly inside a memory root."""
    try:
        real = os.path.realpath(path)
    except Exception:
        return False
    if not real.endswith(".md"):
        return False
    for root in memory_roots():
        rroot = os.path.realpath(root)
        if os.path.dirname(real) == rroot:
            return True
    return False


def parse_frontmatter(text):
    """
    Best-effort parse of the YAML-ish frontmatter block.
    Returns (meta_dict, body_str). Handles both flat keys and a nested
    `metadata:` block (type lives under metadata in the newer format).
    """
    meta = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            block = text[3:end].strip("\n")
            # body starts after the closing '---' line
            rest = text[end + 4:]
            body = rest.lstrip("\n")
            in_metadata = False
            for line in block.splitlines():
                if not line.strip():
                    continue
                indented = line[0] in (" ", "\t")
                stripped = line.strip()
                if stripped.endswith(":") and stripped[:-1] in (
                    "metadata",
                ):
                    in_metadata = True
                    continue
                if ":" in stripped:
                    key, _, val = stripped.partition(":")
                    key = key.strip()
                    val = val.strip()
                    if indented and in_metadata:
                        meta.setdefault("metadata", {})[key] = val
                    else:
                        in_metadata = False
                        meta[key] = val
    # Surface a single 'type' regardless of where it lives.
    mtype = meta.get("type")
    if not mtype and isinstance(meta.get("metadata"), dict):
        mtype = meta["metadata"].get("type")
    meta["_type"] = mtype or "unknown"
    return meta, body


def list_all():
    projects = []
    for root in memory_roots():
        dirname = os.path.basename(os.path.dirname(root))
        label, decoded = split_project(dirname)
        files = []
        for fn in sorted(os.listdir(root)):
            if not fn.endswith(".md"):
                continue
            full = os.path.join(root, fn)
            if not os.path.isfile(full):
                continue
            try:
                with open(full, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except Exception:
                text = ""
            meta, _ = parse_frontmatter(text)
            st = os.stat(full)
            files.append({
                "filename": fn,
                "path": full,
                "title": meta.get("name") or fn[:-3],
                "description": meta.get("description", ""),
                "type": meta.get("_type", "unknown"),
                "isIndex": fn == "MEMORY.md",
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
        # Sort: MEMORY.md first, then alphabetical by title.
        files.sort(key=lambda f: (not f["isIndex"], f["title"].lower()))
        projects.append({
            "dirname": dirname,
            "path": decoded,
            "label": label,
            "files": files,
        })
    return projects


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _qs(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def do_GET(self):
        route = urllib.parse.urlparse(self.path).path
        if route == "/":
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif route == "/api/memories":
            self._send(200, {"projects": list_all()})
        elif route == "/api/memory":
            path = (self._qs().get("path") or [""])[0]
            if not is_safe_path(path) or not os.path.isfile(path):
                return self._send(404, {"error": "not found"})
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
            self._send(200, {"path": path, "content": content})
        else:
            self._send(404, {"error": "unknown route"})

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._send(400, {"error": "bad json"})
        if route == "/api/memory":
            path = data.get("path", "")
            content = data.get("content", "")
            if not is_safe_path(path):
                return self._send(403, {"error": "path not allowed"})
            if not os.path.isdir(os.path.dirname(path)):
                return self._send(404, {"error": "dir missing"})
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "unknown route"})

    def do_DELETE(self):
        route = urllib.parse.urlparse(self.path).path
        if route == "/api/memory":
            path = (self._qs().get("path") or [""])[0]
            if not is_safe_path(path):
                return self._send(403, {"error": "path not allowed"})
            if os.path.basename(path) == "MEMORY.md":
                return self._send(
                    403, {"error": "refusing to delete the MEMORY.md index"}
                )
            if not os.path.isfile(path):
                return self._send(404, {"error": "not found"})
            os.remove(path)
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "unknown route"})


# --------------------------------------------------------------------------- #
# Frontend (single embedded HTML doc — no external assets)
# --------------------------------------------------------------------------- #
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Memory Viewer</title>
<style>
  :root {
    --bg: #1a1816; --panel: #242019; --panel2: #2c281f; --border: #3a3428;
    --text: #ece6da; --muted: #9b9384; --accent: #d97757; --accent2: #c4633f;
    --green: #7fa650; --blue: #6a9bc3; --purple: #a98bc9; --yellow: #d4a843;
    --danger: #d96a5a;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; }
  body {
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    background: var(--bg); color: var(--text); display: flex; flex-direction: column;
  }
  header {
    padding: 12px 18px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 12px; background: var(--panel);
  }
  header h1 { font-size: 15px; margin: 0; font-weight: 600; letter-spacing: .2px; }
  header .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--accent); }
  header .spacer { flex: 1; }
  header .stat { color: var(--muted); font-size: 12px; }
  header button {
    background: var(--panel2); color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 5px 10px; cursor: pointer; font-size: 12px;
  }
  header button:hover { border-color: var(--accent); }

  .layout { flex: 1; display: flex; min-height: 0; }
  .sidebar {
    width: 320px; min-width: 240px; max-width: 480px; border-right: 1px solid var(--border);
    overflow-y: auto; background: var(--panel); resize: horizontal;
  }
  .search { padding: 10px; position: sticky; top: 0; background: var(--panel); border-bottom: 1px solid var(--border); }
  .search input {
    width: 100%; padding: 7px 10px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--bg); color: var(--text); font-size: 13px;
  }
  .search input:focus { outline: none; border-color: var(--accent); }

  .project { border-bottom: 1px solid var(--border); }
  .project > .phead {
    padding: 9px 12px; cursor: pointer; display: flex; align-items: center; gap: 8px;
    user-select: none;
  }
  .project > .phead:hover { background: var(--panel2); }
  .project .caret { color: var(--muted); font-size: 10px; width: 10px; transition: transform .1s; }
  .project.collapsed .caret { transform: rotate(-90deg); }
  .project .pname { font-weight: 600; font-size: 13px; }
  .project .ppath { color: var(--muted); font-size: 11px; word-break: break-all; }
  .project .pcount { margin-left: auto; color: var(--muted); font-size: 11px;
    background: var(--bg); border-radius: 10px; padding: 1px 8px; }
  .project.collapsed .files { display: none; }

  .file {
    padding: 8px 12px 8px 30px; cursor: pointer; border-top: 1px solid rgba(255,255,255,.03);
  }
  .file:hover { background: var(--panel2); }
  .file.active { background: var(--accent); color: #1a1816; }
  .file.active .fdesc, .file.active .ftype { color: #2c1a12; }
  .file .frow { display: flex; align-items: center; gap: 8px; }
  .file .ftitle { font-weight: 500; font-size: 13px; flex: 1; word-break: break-word; }
  .file .fdesc { color: var(--muted); font-size: 11px; margin-top: 2px; }
  .badge {
    font-size: 10px; padding: 1px 7px; border-radius: 10px; font-weight: 600;
    text-transform: uppercase; letter-spacing: .3px; flex-shrink: 0;
  }
  .badge.user { background: rgba(106,155,195,.2); color: var(--blue); }
  .badge.feedback { background: rgba(217,119,87,.2); color: var(--accent); }
  .badge.project { background: rgba(127,166,80,.2); color: var(--green); }
  .badge.reference { background: rgba(169,139,201,.2); color: var(--purple); }
  .badge.index { background: rgba(212,168,67,.2); color: var(--yellow); }
  .badge.unknown { background: rgba(155,147,132,.2); color: var(--muted); }

  main { flex: 1; overflow-y: auto; min-width: 0; }
  .empty { color: var(--muted); text-align: center; margin-top: 18vh; padding: 0 30px; }
  .empty svg { opacity: .4; }

  .doc { max-width: 860px; margin: 0 auto; padding: 26px 34px 80px; }
  .doc .toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; flex-wrap: wrap; }
  .doc .toolbar .spacer { flex: 1; }
  .doc h2.title { font-size: 21px; margin: 0 0 2px; }
  .doc .meta-path { color: var(--muted); font-size: 12px; word-break: break-all; margin-bottom: 18px; }
  .btn {
    border: 1px solid var(--border); background: var(--panel2); color: var(--text);
    border-radius: 6px; padding: 6px 13px; cursor: pointer; font-size: 13px;
  }
  .btn:hover { border-color: var(--accent); }
  .btn.primary { background: var(--accent); border-color: var(--accent); color: #1a1816; font-weight: 600; }
  .btn.primary:hover { background: var(--accent2); }
  .btn.danger { color: var(--danger); }
  .btn.danger:hover { border-color: var(--danger); background: rgba(217,106,90,.12); }
  .btn:disabled { opacity: .5; cursor: default; }

  .rendered { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 22px 26px; }
  .rendered h1,.rendered h2,.rendered h3 { line-height: 1.3; }
  .rendered h1 { font-size: 19px; } .rendered h2 { font-size: 17px; } .rendered h3 { font-size: 15px; }
  .rendered code { background: var(--bg); padding: 1px 5px; border-radius: 4px; font-size: 12.5px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .rendered pre { background: var(--bg); padding: 12px 14px; border-radius: 8px; overflow-x: auto; border: 1px solid var(--border); }
  .rendered pre code { background: none; padding: 0; }
  .rendered a { color: var(--accent); }
  .rendered .wikilink { color: var(--blue); background: rgba(106,155,195,.13); padding: 1px 6px; border-radius: 4px; text-decoration: none; }
  .rendered ul, .rendered ol { padding-left: 22px; }
  .rendered blockquote { border-left: 3px solid var(--border); margin: 0; padding-left: 14px; color: var(--muted); }
  .rendered hr { border: none; border-top: 1px solid var(--border); margin: 18px 0; }
  .rendered table { border-collapse: collapse; }
  .rendered td, .rendered th { border: 1px solid var(--border); padding: 5px 10px; }

  .fmcard { background: var(--panel2); border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 14px; margin-bottom: 14px; font-size: 12.5px; }
  .fmcard .row { display: flex; gap: 8px; padding: 2px 0; }
  .fmcard .k { color: var(--muted); min-width: 92px; }
  .fmcard .v { word-break: break-word; }

  textarea#editor {
    width: 100%; min-height: 60vh; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 10px; padding: 16px;
    font: 13px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; resize: vertical;
  }
  textarea#editor:focus { outline: none; border-color: var(--accent); }

  .toast {
    position: fixed; bottom: 22px; left: 50%; transform: translateX(-50%);
    background: var(--panel2); border: 1px solid var(--border); color: var(--text);
    padding: 9px 18px; border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity .2s;
    pointer-events: none; z-index: 50; box-shadow: 0 6px 20px rgba(0,0,0,.4);
  }
  .toast.show { opacity: 1; }
  .toast.err { border-color: var(--danger); color: var(--danger); }

  .modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,.55); display: none;
    align-items: center; justify-content: center; z-index: 60; }
  .modal-bg.show { display: flex; }
  .modal { background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 22px 24px; max-width: 420px; }
  .modal h3 { margin: 0 0 8px; }
  .modal p { color: var(--muted); margin: 0 0 18px; }
  .modal .actions { display: flex; gap: 10px; justify-content: flex-end; }
</style>
</head>
<body>
<header>
  <span class="dot"></span>
  <h1>Claude Memory Viewer</h1>
  <span class="spacer"></span>
  <span class="stat" id="stat"></span>
  <button id="refresh">↻ Refresh</button>
</header>

<div class="layout">
  <aside class="sidebar">
    <div class="search"><input id="search" placeholder="Filter projects & memories…" autocomplete="off"></div>
    <div id="tree"></div>
  </aside>
  <main>
    <div id="view"></div>
  </main>
</div>

<div class="toast" id="toast"></div>
<div class="modal-bg" id="modalBg">
  <div class="modal">
    <h3 id="modalTitle">Delete memory?</h3>
    <p id="modalBody"></p>
    <div class="actions">
      <button class="btn" id="modalCancel">Cancel</button>
      <button class="btn danger" id="modalConfirm">Delete</button>
    </div>
  </div>
</div>

<script>
const $ = (s, el=document) => el.querySelector(s);
let DATA = { projects: [] };
let current = null;       // selected file: {projectIdx, path, filename, title, isIndex, content}
let editing = false;
let dirty = false;

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(url, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

function toast(msg, isErr) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(t._t);
  t._t = setTimeout(() => t.className = 'toast', 2200);
}

// ---- minimal, safe markdown renderer ----
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function inlineMd(s){
  s = esc(s);
  s = s.replace(/`([^`]+)`/g, (_,c)=>'<code>'+c+'</code>');
  s = s.replace(/\[\[([^\]]+)\]\]/g, (_,c)=>'<span class="wikilink">'+c+'</span>');
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_,t,u)=>'<a href="'+u+'" target="_blank" rel="noopener">'+t+'</a>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
  return s;
}
function renderMd(src){
  const lines = src.split('\n');
  let html = '', i = 0;
  while (i < lines.length) {
    let line = lines[i];
    if (/^```/.test(line)) {
      let buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(esc(lines[i])); i++; }
      i++; html += '<pre><code>' + buf.join('\n') + '</code></pre>'; continue;
    }
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) { const n=m[1].length; html += `<h${n}>${inlineMd(m[2])}</h${n}>`; i++; continue; }
    if (/^\s*([-*+])\s+/.test(line)) {
      html += '<ul>';
      while (i < lines.length && /^\s*([-*+])\s+/.test(lines[i])) { html += '<li>' + inlineMd(lines[i].replace(/^\s*([-*+])\s+/,'')) + '</li>'; i++; }
      html += '</ul>'; continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      html += '<ol>';
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) { html += '<li>' + inlineMd(lines[i].replace(/^\s*\d+\.\s+/,'')) + '</li>'; i++; }
      html += '</ol>'; continue;
    }
    if (/^\s*>/.test(line)) { html += '<blockquote>' + inlineMd(line.replace(/^\s*>\s?/,'')) + '</blockquote>'; i++; continue; }
    if (/^\s*---+\s*$/.test(line)) { html += '<hr>'; i++; continue; }
    if (line.trim() === '') { i++; continue; }
    let para = [line]; i++;
    while (i < lines.length && lines[i].trim() !== '' && !/^(#{1,6}\s|```|\s*[-*+]\s|\s*\d+\.\s|\s*>)/.test(lines[i])) { para.push(lines[i]); i++; }
    html += '<p>' + inlineMd(para.join('\n')).replace(/\n/g,'<br>') + '</p>';
  }
  return html;
}

function splitFrontmatter(text){
  if (text.startsWith('---')) {
    const end = text.indexOf('\n---', 3);
    if (end !== -1) {
      const block = text.slice(3, end).replace(/^\n/,'');
      const body = text.slice(end + 4).replace(/^\n+/,'');
      const meta = [];
      block.split('\n').forEach(l => { if (l.trim()) meta.push(l); });
      return { metaLines: meta, body };
    }
  }
  return { metaLines: [], body: text };
}

// ---- sidebar ----
function renderTree(){
  const q = $('#search').value.toLowerCase().trim();
  const tree = $('#tree');
  tree.innerHTML = '';
  DATA.projects.forEach((p, pi) => {
    const matchFiles = p.files.filter(f => {
      if (!q) return true;
      return (f.title + ' ' + f.description + ' ' + f.filename + ' ' + f.type).toLowerCase().includes(q)
          || p.label.toLowerCase().includes(q) || p.path.toLowerCase().includes(q);
    });
    if (q && matchFiles.length === 0) return;
    const filesToShow = q ? matchFiles : p.files;
    const el = document.createElement('div');
    el.className = 'project' + (p._collapsed ? ' collapsed' : '');
    const phead = document.createElement('div');
    phead.className = 'phead';
    phead.innerHTML = `<span class="caret">▼</span>
      <div style="min-width:0"><div class="pname">${escapeHtml(p.label)}</div>
      <div class="ppath">${escapeHtml(p.path)}</div></div>
      <span class="pcount">${p.files.length}</span>`;
    phead.onclick = () => { p._collapsed = !p._collapsed; renderTree(); };
    el.appendChild(phead);
    const fl = document.createElement('div');
    fl.className = 'files';
    filesToShow.forEach(f => {
      const fe = document.createElement('div');
      const active = current && current.path === f.path;
      fe.className = 'file' + (active ? ' active' : '');
      const badgeType = f.isIndex ? 'index' : f.type;
      const badgeLabel = f.isIndex ? 'index' : f.type;
      fe.innerHTML = `<div class="frow"><span class="ftitle">${escapeHtml(f.title)}</span>
        <span class="badge ${badgeType}">${escapeHtml(badgeLabel)}</span></div>
        ${f.description ? `<div class="fdesc">${escapeHtml(f.description)}</div>` : ''}`;
      fe.onclick = () => openFile(pi, f);
      fl.appendChild(fe);
    });
    el.appendChild(fl);
    tree.appendChild(el);
  });
  const total = DATA.projects.reduce((a,p)=>a+p.files.length,0);
  $('#stat').textContent = `${DATA.projects.length} projects · ${total} memories`;
  if (DATA.projects.length === 0) tree.innerHTML = '<div class="empty" style="margin-top:30px">No memory files found.</div>';
}
function escapeHtml(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

// ---- main pane ----
function showEmpty(){
  $('#view').innerHTML = `<div class="empty">
    <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 4h12l4 4v12H4z"/><path d="M16 4v4h4"/><path d="M8 12h8M8 16h6"/></svg>
    <p>Select a memory file from the left to view or edit it.</p></div>`;
}

async function openFile(pi, f){
  if (dirty && !confirm('Discard unsaved changes?')) return;
  editing = false; dirty = false;
  current = { projectIdx: pi, path: f.path, filename: f.filename, title: f.title, isIndex: f.isIndex };
  renderTree();
  let content = '';
  try { const r = await api('GET', '/api/memory?path=' + encodeURIComponent(f.path)); content = r.content; }
  catch(e){ toast('Load failed: ' + e.message, true); return; }
  current.content = content;
  renderView();
}

function renderView(){
  const f = current;
  const { metaLines, body } = splitFrontmatter(f.content);
  const fmHtml = metaLines.length ? `<div class="fmcard">${metaLines.map(l=>{
      const idx = l.indexOf(':');
      if (idx === -1) return `<div class="row"><span class="v">${escapeHtml(l)}</span></div>`;
      const k = l.slice(0,idx), v = l.slice(idx+1);
      return `<div class="row"><span class="k">${escapeHtml(k.trim())}</span><span class="v">${escapeHtml(v.trim())}</span></div>`;
    }).join('')}</div>` : '';

  const view = $('#view');
  view.innerHTML = `<div class="doc">
    <div class="toolbar">
      <h2 class="title">${escapeHtml(f.title)}</h2>
      <span class="spacer"></span>
      ${editing
        ? `<button class="btn" id="cancelBtn">Cancel</button><button class="btn primary" id="saveBtn">Save</button>`
        : `<button class="btn" id="editBtn">✎ Edit</button>${f.isIndex ? '' : '<button class="btn danger" id="delBtn">🗑 Delete</button>'}`}
    </div>
    <div class="meta-path">${escapeHtml(f.path)}</div>
    ${editing
      ? `<textarea id="editor" spellcheck="false">${escapeHtml(f.content)}</textarea>`
      : `${fmHtml}<div class="rendered">${renderMd(body)}</div>`}
  </div>`;

  if (editing) {
    $('#editor').addEventListener('input', () => dirty = true);
    $('#saveBtn').onclick = save;
    $('#cancelBtn').onclick = () => { if (dirty && !confirm('Discard changes?')) return; editing=false; dirty=false; renderView(); };
    $('#editor').focus();
  } else {
    $('#editBtn').onclick = () => { editing = true; renderView(); };
    const del = $('#delBtn'); if (del) del.onclick = confirmDelete;
  }
}

async function save(){
  const content = $('#editor').value;
  try {
    await api('POST', '/api/memory', { path: current.path, content });
    current.content = content; dirty = false; editing = false;
    toast('Saved');
    await reload(true);     // refresh sidebar metadata
    renderView();
  } catch(e){ toast('Save failed: ' + e.message, true); }
}

function confirmDelete(){
  $('#modalBody').textContent = `“${current.title}” (${current.filename}) will be permanently deleted from disk.`;
  $('#modalBg').classList.add('show');
}
$('#modalCancel').onclick = () => $('#modalBg').classList.remove('show');
$('#modalConfirm').onclick = async () => {
  $('#modalBg').classList.remove('show');
  try {
    await api('DELETE', '/api/memory?path=' + encodeURIComponent(current.path));
    toast('Deleted');
    current = null;
    await reload(true);
    showEmpty();
  } catch(e){ toast('Delete failed: ' + e.message, true); }
};

async function reload(keepView){
  const collapsed = {};
  DATA.projects.forEach(p => collapsed[p.dirname] = p._collapsed);
  const r = await api('GET', '/api/memories');
  DATA = r;
  // Collapse projects by default; preserve any state the user already toggled.
  DATA.projects.forEach(p => {
    p._collapsed = (p.dirname in collapsed) ? collapsed[p.dirname] : true;
  });
  renderTree();
  if (!keepView && !current) showEmpty();
}

$('#refresh').onclick = () => reload();
$('#search').addEventListener('input', renderTree);
window.addEventListener('beforeunload', e => { if (dirty){ e.preventDefault(); e.returnValue=''; } });

(async () => { await reload(); showEmpty(); })();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Local Claude memory viewer")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument(
        "--root",
        default=None,
        help="Path to your Claude projects dir (default: ~/.claude/projects, "
             "or the CLAUDE_PROJECTS_ROOT env var).",
    )
    args = ap.parse_args()

    if args.root:
        global PROJECTS_ROOT
        PROJECTS_ROOT = os.path.expanduser(args.root)

    roots = memory_roots()
    print(f"Claude Memory Viewer")
    print(f"  projects root : {PROJECTS_ROOT}")
    print(f"  memory dirs   : {len(roots)} found")
    url = f"http://{args.host}:{args.port}/"
    print(f"  serving       : {url}")
    print(f"  (Ctrl+C to stop)")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(0.6), webbrowser.open(url)), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        httpd.shutdown()


if __name__ == "__main__":
    main()
