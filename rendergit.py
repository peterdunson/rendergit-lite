#!/usr/bin/env python3
"""
rendergit-lite: Interactive GitHub repo viewer with selectable folders/files and improved UI.
Fork of rendergit by Andrej Karpathy with interactive selection features.
"""

from __future__ import annotations
import argparse
import html
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from dataclasses import dataclass
from typing import List, Set

# External deps
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_for_filename, TextLexer
import markdown

MAX_DEFAULT_BYTES = 50 * 1024

# Common bloat files to auto-skip
BLOAT_PATTERNS = {
    "package-lock.json", "yarn.lock", "poetry.lock", "Cargo.lock", "pnpm-lock.yaml",
    "Gemfile.lock", "composer.lock", "Pipfile.lock", "go.sum",
}

BLOAT_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", ".nuxt", "target", "out", ".gradle", ".cache",
}

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".ogg", ".flac",
    ".ttf", ".otf", ".eot", ".woff", ".woff2",
    ".so", ".dll", ".dylib", ".class", ".jar", ".exe", ".bin",
}

MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdown", ".mkd", ".mkdn"}

# File type icons
FILE_ICONS = {
    ".py": "🐍",
    ".js": "💛",
    ".jsx": "⚛️",
    ".ts": "🔷",
    ".tsx": "⚛️",
    ".html": "🌐",
    ".css": "🎨",
    ".scss": "🎨",
    ".json": "📋",
    ".md": "📄",
    ".txt": "📝",
    ".yaml": "⚙️",
    ".yml": "⚙️",
    ".toml": "⚙️",
    ".xml": "📰",
    ".sh": "🔧",
    ".go": "🐹",
    ".rs": "🦀",
    ".java": "☕",
    ".cpp": "⚡",
    ".c": "⚡",
    ".rb": "💎",
    ".php": "🐘",
    ".swift": "🦅",
    ".kt": "🟣",
}

@dataclass
class RenderDecision:
    include: bool
    reason: str  # "ok" | "binary" | "too_large" | "ignored" | "bloat"

@dataclass
class FileInfo:
    path: pathlib.Path  # absolute path on disk
    rel: str            # path relative to repo root (slash-separated)
    size: int
    decision: RenderDecision


def run(cmd: List[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def git_clone(url: str, dst: str) -> None:
    run(["git", "clone", "--depth", "1", url, dst])


def git_head_commit(repo_dir: str) -> str:
    try:
        cp = run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
        return cp.stdout.strip()
    except Exception:
        return "(unknown)"


def bytes_human(n: int) -> str:
    """Human-readable bytes: 1 decimal for KiB and above, integer for B."""
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    f = float(n)
    i = 0
    while f >= 1024.0 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    if i == 0:
        return f"{int(f)} {units[i]}"
    else:
        return f"{f:.1f} {units[i]}"


def looks_binary(path: pathlib.Path) -> bool:
    ext = path.suffix.lower()
    if ext in BINARY_EXTENSIONS:
        return True
    try:
        with path.open("rb") as f:
            chunk = f.read(8192)
        if b"\x00" in chunk:
            return True
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError:
            return True
        return False
    except Exception:
        return True


def is_bloat(rel_path: str, skip_bloat: bool) -> bool:
    """Check if file is common bloat (lock files, node_modules, etc.)"""
    if not skip_bloat:
        return False
    
    # Check filename
    filename = os.path.basename(rel_path)
    if filename in BLOAT_PATTERNS:
        return True
    
    # Check if in bloat directory
    parts = rel_path.split("/")
    for part in parts:
        if part in BLOAT_DIRS:
            return True
    
    return False


def decide_file(path: pathlib.Path, repo_root: pathlib.Path, max_bytes: int, skip_bloat: bool) -> FileInfo:
    rel = str(path.relative_to(repo_root)).replace(os.sep, "/")
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    
    # Ignore VCS
    if "/.git/" in f"/{rel}/" or rel.startswith(".git/"):
        return FileInfo(path, rel, size, RenderDecision(False, "ignored"))
    
    # Check for bloat
    if is_bloat(rel, skip_bloat):
        return FileInfo(path, rel, size, RenderDecision(False, "bloat"))
    
    if size > max_bytes:
        return FileInfo(path, rel, size, RenderDecision(False, "too_large"))
    
    if looks_binary(path):
        return FileInfo(path, rel, size, RenderDecision(False, "binary"))
    
    return FileInfo(path, rel, size, RenderDecision(True, "ok"))


def collect_files(repo_root: pathlib.Path, max_bytes: int, skip_bloat: bool) -> List[FileInfo]:
    infos: List[FileInfo] = []
    for p in sorted(repo_root.rglob("*")):
        if p.is_symlink():
            continue
        if p.is_file():
            infos.append(decide_file(p, repo_root, max_bytes, skip_bloat))
    return infos


def read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def render_markdown_text(md_text: str) -> str:
    return markdown.markdown(md_text, extensions=["fenced_code", "tables", "toc"])  # type: ignore


def highlight_code(text: str, filename: str, formatter: HtmlFormatter) -> str:
    try:
        lexer = get_lexer_for_filename(filename, stripall=False)
    except Exception:
        lexer = TextLexer(stripall=False)
    return highlight(text, lexer, formatter)


def slugify(path_str: str) -> str:
    out = []
    for ch in path_str:
        if ch.isalnum() or ch in {"-", "_"}:
            out.append(ch)
        else:
            out.append("-")
    return "".join(out)


def get_file_icon(filename: str) -> str:
    """Get emoji icon for file type"""
    ext = os.path.splitext(filename)[1].lower()
    return FILE_ICONS.get(ext, "📄")


def build_folder_tree(infos: List[FileInfo]) -> str:
    """Build interactive checkbox tree for file selection"""
    rendered = [i for i in infos if i.decision.include]
    
    # Build folder structure
    folders = {}
    for info in rendered:
        parts = info.rel.split("/")
        current = folders
        for i, part in enumerate(parts[:-1]):
            if part not in current:
                current[part] = {}
            current = current[part]
    
    # Generate HTML tree
    def render_tree(tree, path="", level=0):
        html_parts = []
        indent = "  " * level
        
        # Get all files in this level
        files_here = [i for i in rendered if "/".join(i.rel.split("/")[:-1]) == path.rstrip("/")]
        
        # Render folders first
        for folder_name in sorted(tree.keys()):
            folder_path = f"{path}/{folder_name}" if path else folder_name
            folder_id = slugify(folder_path)
            
            html_parts.append(f'{indent}<div class="tree-folder" data-level="{level}">')
            html_parts.append(f'{indent}  <label>')
            html_parts.append(f'{indent}    <input type="checkbox" class="folder-checkbox" data-folder="{folder_path}" checked>')
            html_parts.append(f'{indent}    <span class="folder-icon">📁</span> <strong>{html.escape(folder_name)}/</strong>')
            html_parts.append(f'{indent}  </label>')
            html_parts.append(f'{indent}  <div class="tree-children">')
            html_parts.append(render_tree(tree[folder_name], folder_path, level + 1))
            html_parts.append(f'{indent}  </div>')
            html_parts.append(f'{indent}</div>')
        
        # Render files
        for info in sorted(files_here, key=lambda x: x.rel.split("/")[-1]):
            file_id = slugify(info.rel)
            icon = get_file_icon(info.rel)
            html_parts.append(f'{indent}<div class="tree-file" data-level="{level}">')
            html_parts.append(f'{indent}  <label>')
            html_parts.append(f'{indent}    <input type="checkbox" class="file-checkbox" data-file="{info.rel}" checked>')
            html_parts.append(f'{indent}    <span class="file-icon">{icon}</span> {html.escape(os.path.basename(info.rel))}')
            html_parts.append(f'{indent}    <span class="muted">({bytes_human(info.size)})</span>')
            html_parts.append(f'{indent}  </label>')
            html_parts.append(f'{indent}</div>')
        
        return "\n".join(html_parts)
    
    return render_tree(folders)


def build_html(repo_url: str, repo_dir: pathlib.Path, head_commit: str, infos: List[FileInfo]) -> str:
    formatter = HtmlFormatter(nowrap=False)
    pygments_css = formatter.get_style_defs('.highlight')

    # Stats
    rendered = [i for i in infos if i.decision.include]
    skipped_binary = [i for i in infos if i.decision.reason == "binary"]
    skipped_large = [i for i in infos if i.decision.reason == "too_large"]
    skipped_bloat = [i for i in infos if i.decision.reason == "bloat"]
    skipped_ignored = [i for i in infos if i.decision.reason == "ignored"]
    total_files = len(rendered) + len(skipped_binary) + len(skipped_large) + len(skipped_bloat) + len(skipped_ignored)

    # Build folder tree
    folder_tree_html = build_folder_tree(infos)

    # Build table of contents for sidebar
    toc_items = []
    for i in rendered:
        anchor = slugify(i.rel)
        icon = get_file_icon(i.rel)
        toc_items.append(
            f'<li><a href="#file-{anchor}"><span class="file-icon">{icon}</span>{html.escape(i.rel)}</a></li>'
        )
    toc_html = "\n".join(toc_items)

    # Render file sections with data attributes for filtering
    sections: List[str] = []
    file_data = []  # For JavaScript
    
    for i in rendered:
        anchor = slugify(i.rel)
        p = i.path
        ext = p.suffix.lower()
        icon = get_file_icon(i.rel)
        
        try:
            text = read_text(p)
            if ext in MARKDOWN_EXTENSIONS:
                body_html = render_markdown_text(text)
            else:
                code_html = highlight_code(text, i.rel, formatter)
                body_html = f'<div class="highlight">{code_html}</div>'
        except Exception as e:
            body_html = f'<pre class="error">Failed to render: {html.escape(str(e))}</pre>'
        
        sections.append(f"""
<section class="file-section" id="file-{anchor}" data-file="{i.rel}">
  <h2>
    <span class="file-icon">{icon}</span>
    {html.escape(i.rel)} 
    <span class="muted">({bytes_human(i.size)})</span>
  </h2>
  <div class="file-body">{body_html}</div>
  <div class="back-top"><a href="#top">↑ Back to top</a></div>
</section>
""")
        
        # Store file data for JavaScript
        file_data.append({
            "path": i.rel,
            "size": i.size,
            "content": text
        })

    # Skips lists
    def render_skip_list(title: str, items: List[FileInfo]) -> str:
        if not items:
            return ""
        lis = [
            f"<li><code>{html.escape(i.rel)}</code> "
            f"<span class='muted'>({bytes_human(i.size)})</span></li>"
            for i in items
        ]
        return (
            f"<details><summary>{html.escape(title)} ({len(items)})</summary>"
            f"<ul class='skip-list'>\n" + "\n".join(lis) + "\n</ul></details>"
        )

    skipped_html = (
        render_skip_list("Skipped bloat files", skipped_bloat) +
        render_skip_list("Skipped binaries", skipped_binary) +
        render_skip_list("Skipped large files", skipped_large)
    )

    # HTML with interactive selection
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>rendergit-lite – {html.escape(repo_url)}</title>
<style>
  * {{
    box-sizing: border-box;
  }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    margin: 0;
    padding: 0;
    line-height: 1.6;
    background: linear-gradient(135deg, #2ecc71 0%, #3498db 100%);
  }}
  .page {{
    max-width: 1400px;
    margin: 0 auto;
    background: white;
    min-height: 100vh;
    box-shadow: 0 0 50px rgba(0,0,0,0.1);
    margin-left: 300px;
    max-width: calc(1400px - 300px);
    transition: margin-left 0.3s ease, max-width 0.3s ease;
  }}

  .page.sidebar-collapsed {{
    margin-left: 0;
    max-width: 1400px;
  }}

  /* Sidebar navigation */
  .sidebar {{
    position: fixed;
    left: 0;
    top: 0;
    width: 300px;
    height: 100vh;
    background: white;
    border-right: 2px solid #e2e8f0;
    overflow-y: auto;
    padding: 1rem;
    z-index: 100;
    transition: transform 0.3s ease;
  }}

  .sidebar.collapsed {{
    transform: translateX(-100%);
  }}

  /* Toggle button */
  .sidebar-toggle {{
    position: fixed;
    top: 10px;
    left: 270px;
    z-index: 101;
    background: #2ecc71;
    color: white;
    border: none;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    cursor: pointer;
    font-size: 12px;
    transition: all 0.3s ease;
    display: flex;
    align-items: center;
    justify-content: center;
  }}

  .sidebar-toggle:hover {{
    background: #27ae60;
  }}

  .sidebar-toggle.sidebar-collapsed {{
    left: 20px;
  }}

  .sidebar h3 {{
    margin: 0 0 1rem 0;
    color: #2d3748;
    font-size: 1.1rem;
  }}

  .sidebar-toc {{
    list-style: none;
    padding: 0;
    margin: 0;
  }}

  .sidebar-toc li {{
    padding: 0.4rem 0;
    border-bottom: 1px solid #f7fafc;
  }}

  .sidebar-toc a {{
    color: #2ecc71;
    text-decoration: none;
    font-size: 0.9rem;
    display: block;
  }}

  .sidebar-toc a:hover {{
    text-decoration: underline;
    color: #27ae60;
  }}

  .sidebar-toc .file-icon {{
    margin-right: 0.5rem;
  }}

  /* Mobile responsive */
  @media (max-width: 768px) {{
    .sidebar {{
      width: 100%;
      transform: translateX(-100%);
    }}
    
    .sidebar:not(.collapsed) {{
      transform: translateX(0);
    }}
    
    .page {{
      margin-left: 0;
      max-width: 1400px;
    }}
    
    .page.sidebar-collapsed {{
      margin-left: 0;
      max-width: 1400px;
    }}
    
    .sidebar-toggle {{
      display: block;
    }}
  }}
  
  /* Header */
  header {{
    background: linear-gradient(135deg, #2ecc71 0%, #3498db 100%);
    color: white;
    padding: 2rem;
    box-shadow: 0 4px 6px rgba(0,0,0,0.1);
  }}
  h1 {{
    margin: 0 0 0.5rem 0;
    font-size: 2rem;
  }}
  .meta {{
    font-size: 0.9rem;
    opacity: 0.95;
  }}
  .meta a {{
    color: white;
    text-decoration: underline;
  }}
  
  /* View toggle */
  .view-toggle {{
    background: white;
    padding: 1.5rem 2rem;
    border-bottom: 2px solid #e2e8f0;
    display: flex;
    gap: 1rem;
    align-items: center;
  }}
  .toggle-btn {{
    padding: 0.65rem 1.5rem;
    border: 2px solid #cbd5e0;
    background: white;
    cursor: pointer;
    border-radius: 8px;
    font-size: 1rem;
    font-weight: 600;
    transition: all 0.2s;
    color: #4a5568;
  }}
  .toggle-btn.active {{
    background: linear-gradient(135deg, #2ecc71 0%, #3498db 100%);
    color: white;
    border-color: transparent;
    box-shadow: 0 4px 10px rgba(46, 204, 113, 0.3);
  }}
  .toggle-btn:hover:not(.active) {{
    background: #f7fafc;
    border-color: #2ecc71;
    color: #2ecc71;
  }}
  
  /* Main content */
  main {{
    padding: 2rem;
  }}
  
  /* Selection panel */
  .selection-panel {{
    background: #f7fafc;
    border: 2px solid #e2e8f0;
    border-radius: 12px;
    padding: 1.5rem;
    margin-bottom: 2rem;
  }}
  .selection-panel h2 {{
    margin: 0 0 1rem 0;
    font-size: 1.3rem;
    color: #2d3748;
  }}
  .selection-stats {{
    background: white;
    padding: 1rem;
    border-radius: 8px;
    margin-bottom: 1rem;
    display: flex;
    gap: 2rem;
    flex-wrap: wrap;
  }}
  .stat-item {{
    display: flex;
    flex-direction: column;
  }}
  .stat-label {{
    font-size: 0.85rem;
    color: #718096;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}
  .stat-value {{
    font-size: 1.5rem;
    font-weight: 700;
    color: #2ecc71;
  }}
  .quick-filters {{
    display: flex;
    gap: 0.5rem;
    margin-bottom: 1rem;
    flex-wrap: wrap;
  }}
  .filter-btn {{
    padding: 0.5rem 1rem;
    border: 1px solid #cbd5e0;
    background: white;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.9rem;
    transition: all 0.2s;
  }}
  .filter-btn:hover {{
    background: #2ecc71;
    color: white;
    border-color: #2ecc71;
  }}
  
  /* Folder tree */
  .folder-tree {{
    background: white;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 1rem;
    max-height: 400px;
    overflow-y: auto;
  }}
  .tree-folder, .tree-file {{
    margin: 0.25rem 0;
  }}
  .tree-folder label, .tree-file label {{
    cursor: pointer;
    display: flex;
    align-items: center;
    padding: 0.25rem;
    border-radius: 4px;
    transition: background 0.2s;
  }}
  .tree-folder label:hover, .tree-file label:hover {{
    background: #f7fafc;
  }}
  .tree-children {{
    margin-left: 1.5rem;
  }}
  .folder-icon, .file-icon {{
    margin: 0 0.5rem;
  }}
  input[type="checkbox"] {{
    margin-right: 0.5rem;
  }}
  
  /* File sections */
  .file-section {{
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    padding: 2rem;
    margin-bottom: 2rem;
    background: white;
    box-shadow: 0 2px 4px rgba(0,0,0,0.05);
  }}
  .file-section.hidden {{
    display: none;
  }}
  .file-section h2 {{
    margin: 0 0 1rem 0;
    font-size: 1.3rem;
    color: #2d3748;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }}
  .file-body {{
    margin-bottom: 1rem;
  }}
  pre {{
    background: #f7fafc;
    padding: 1rem;
    overflow: auto;
    border-radius: 8px;
    border-left: 4px solid #2ecc71;
  }}
  code {{
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  }}
  .highlight {{
    overflow-x: auto;
  }}
  .back-top {{
    font-size: 0.9rem;
    margin-top: 1rem;
  }}
  .back-top a {{
    color: #2ecc71;
    text-decoration: none;
  }}
  .back-top a:hover {{
    text-decoration: underline;
  }}
  .muted {{
    color: #718096;
    font-weight: normal;
    font-size: 0.9em;
  }}
  .skip-list {{
    list-style: none;
    padding-left: 1rem;
  }}
  .skip-list code {{
    background: #f7fafc;
    padding: 0.1rem 0.3rem;
    border-radius: 4px;
  }}
  details {{
    margin: 1rem 0;
    padding: 0.5rem;
    background: #f7fafc;
    border-radius: 6px;
  }}
  summary {{
    cursor: pointer;
    font-weight: 600;
    padding: 0.5rem;
  }}
  summary:hover {{
    background: #edf2f7;
    border-radius: 4px;
  }}
  
  /* LLM view */
  #llm-view {{
    display: none;
  }}
  .llm-section {{
    background: white;
    padding: 2rem;
    border-radius: 12px;
    border: 1px solid #e2e8f0;
  }}
  .llm-section h2 {{
    margin-top: 0;
    color: #2d3748;
  }}
  #llm-text {{
    width: 100%;
    height: 70vh;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.9rem;
    border: 2px solid #cbd5e0;
    border-radius: 12px;
    padding: 1.5rem;
    resize: vertical;
    background: #f7fafc;
    color: #2d3748;
  }}
  .copy-hint {{
    margin-top: 1rem;
    padding: 1rem;
    background: linear-gradient(135deg, rgba(46, 204, 113, 0.1) 0%, rgba(52, 152, 219, 0.1) 100%);
    border-left: 4px solid #2ecc71;
    border-radius: 8px;
  }}
  kbd {{
    background: #edf2f7;
    border: 1px solid #cbd5e0;
    border-radius: 4px;
    padding: 0.15rem 0.4rem;
    font-family: monospace;
    font-size: 0.85em;
  }}
  
  {pygments_css}
</style>
</head>
<body>
<a id="top"></a>

<!-- Sidebar toggle button -->
<button class="sidebar-toggle" onclick="toggleSidebar()">◀</button>

<!-- Sidebar navigation -->
<div class="sidebar" id="sidebar">
  <h3>📑 Files ({len(rendered)})</h3>
  <ul class="sidebar-toc">
    {toc_html}
  </ul>
</div>

<div class="page">
  <header>
    <h1>🚀 {html.escape(os.path.basename(repo_url.rstrip('/').rstrip('.git')))}</h1>
    <div class="meta">
      <div><strong>Repository:</strong> <a href="{html.escape(repo_url)}">{html.escape(repo_url)}</a></div>
      <div><strong>Commit:</strong> {html.escape(head_commit[:8])}</div>
      <div><strong>Total files:</strong> {total_files} · <strong>Rendered:</strong> {len(rendered)} · <strong>Skipped:</strong> {len(skipped_binary) + len(skipped_large) + len(skipped_bloat) + len(skipped_ignored)}</div>
    </div>
  </header>

  <div class="view-toggle">
    <strong>View:</strong>
    <button class="toggle-btn active" onclick="showHumanView(this)">👤 Human</button>
    <button class="toggle-btn" onclick="showLLMView(this)">🤖 LLM</button>
  </div>

  <main>
    <div id="human-view">
      <div class="selection-panel">
        <h2>📁 Select Files to Include</h2>
        
        <div class="selection-stats">
          <div class="stat-item">
            <span class="stat-label">Selected Files</span>
            <span class="stat-value" id="selected-count">{len(rendered)}</span>
          </div>
          <div class="stat-item">
            <span class="stat-label">Total Size</span>
            <span class="stat-value" id="selected-size">{bytes_human(sum(i.size for i in rendered))}</span>
          </div>
        </div>
        
        <div class="quick-filters">
          <button class="filter-btn" onclick="selectAll()">✅ Select All</button>
          <button class="filter-btn" onclick="deselectAll()">❌ Deselect All</button>
          <button class="filter-btn" onclick="filterByExtension('.py')">🐍 Python Only</button>
          <button class="filter-btn" onclick="filterByExtension('.js')">💛 JavaScript Only</button>
          <button class="filter-btn" onclick="filterByExtension('.md')">📄 Markdown Only</button>
          <button class="filter-btn" onclick="toggleTests()">🧪 Toggle Tests</button>
        </div>
        
        <div class="folder-tree" id="folder-tree">
          {folder_tree_html}
        </div>
      </div>
      
      <section>
        <h2>Skipped items</h2>
        {skipped_html}
      </section>

      <div id="file-sections">
        {''.join(sections)}
      </div>
    </div>

    <div id="llm-view">
      <div class="llm-section">
        <h2>🤖 LLM View - CXML Format</h2>
        <p>This view updates based on your file selection. Copy and paste to an LLM for analysis:</p>
        <textarea id="llm-text" readonly></textarea>
        <div class="copy-hint">
          💡 <strong>Tip:</strong> Click in the text area, press <kbd>Ctrl+A</kbd> (or <kbd>Cmd+A</kbd>), then <kbd>Ctrl+C</kbd> (or <kbd>Cmd+C</kbd>) to copy.
        </div>
      </div>
    </div>
  </main>
</div>

<script>
// File data embedded from Python
const fileData = {json.dumps(file_data)};

// Update stats and LLM view based on selection
function updateSelection() {{
  const selectedFiles = [];
  const checkboxes = document.querySelectorAll('.file-checkbox:checked');
  
  checkboxes.forEach(cb => {{
    const filePath = cb.dataset.file;
    const fileInfo = fileData.find(f => f.path === filePath);
    if (fileInfo) {{
      selectedFiles.push(fileInfo);
    }}
  }});
  
  // Update stats
  document.getElementById('selected-count').textContent = selectedFiles.length;
  const totalSize = selectedFiles.reduce((sum, f) => sum + f.size, 0);
  document.getElementById('selected-size').textContent = formatBytes(totalSize);
  
  // Update visible sections
  document.querySelectorAll('.file-section').forEach(section => {{
    const filePath = section.dataset.file;
    const isChecked = Array.from(checkboxes).some(cb => cb.dataset.file === filePath);
    section.classList.toggle('hidden', !isChecked);
  }});
  
  // Update LLM text
  updateLLMText(selectedFiles);
}}

function updateLLMText(selectedFiles) {{
  let cxml = '<documents>\\n';
  selectedFiles.forEach((file, index) => {{
    cxml += `<document index="${{index + 1}}">\\n`;
    cxml += `<source>${{file.path}}</source>\\n`;
    cxml += '<document_content>\\n';
    cxml += file.content;
    cxml += '\\n</document_content>\\n';
    cxml += '</document>\\n';
  }});
  cxml += '</documents>';
  document.getElementById('llm-text').value = cxml;
}}

function formatBytes(bytes) {{
  const units = ['B', 'KiB', 'MiB', 'GiB'];
  let size = bytes;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {{
    size /= 1024;
    unitIndex++;
  }}
  return unitIndex === 0 ? `${{Math.floor(size)}} ${{units[unitIndex]}}` : `${{size.toFixed(1)}} ${{units[unitIndex]}}`;
}}

// Quick filter functions
function selectAll() {{
  document.querySelectorAll('.file-checkbox, .folder-checkbox').forEach(cb => cb.checked = true);
  updateSelection();
}}

function deselectAll() {{
  document.querySelectorAll('.file-checkbox, .folder-checkbox').forEach(cb => cb.checked = false);
  updateSelection();
}}



function filterByExtension(ext) {{
  deselectAll();
  document.querySelectorAll('.file-checkbox').forEach(cb => {{
    const filePath = cb.dataset.file;
    if (filePath.endsWith(ext)) {{
      cb.checked = true;
    }}
  }});
  updateSelection();
}}

function toggleTests() {{
  document.querySelectorAll('.file-checkbox').forEach(cb => {{
    const filePath = cb.dataset.file;
    if (filePath.includes('/test') || filePath.includes('/tests') || filePath.includes('_test.') || filePath.includes('.test.')) {{
      cb.checked = !cb.checked;
    }}
  }});
  updateSelection();
}}

// Folder checkbox logic - check/uncheck all files in folder
document.querySelectorAll('.folder-checkbox').forEach(folderCb => {{
  folderCb.addEventListener('change', function() {{
    const folderPath = this.dataset.folder;
    const isChecked = this.checked;
    
    // Update all files in this folder
    document.querySelectorAll('.file-checkbox').forEach(fileCb => {{
      const filePath = fileCb.dataset.file;
      if (filePath.startsWith(folderPath + '/') || filePath === folderPath) {{
        fileCb.checked = isChecked;
      }}
    }});
    
    updateSelection();
  }});
}});

// File checkbox change
document.querySelectorAll('.file-checkbox').forEach(cb => {{
  cb.addEventListener('change', updateSelection);
}});

// View switching
function showHumanView(btn) {{
  document.getElementById('human-view').style.display = 'block';
  document.getElementById('llm-view').style.display = 'none';
  document.querySelectorAll('.toggle-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}}

function showLLMView(btn) {{
  document.getElementById('human-view').style.display = 'none';
  document.getElementById('llm-view').style.display = 'block';
  document.querySelectorAll('.toggle-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  
  // Auto-select text
  setTimeout(() => {{
    const textArea = document.getElementById('llm-text');
    textArea.focus();
    textArea.select();
  }}, 100);
}}

// Sidebar toggle functionality
function toggleSidebar() {{
  const sidebar = document.getElementById('sidebar');
  const page = document.querySelector('.page');
  const toggleBtn = document.querySelector('.sidebar-toggle');
  
  sidebar.classList.toggle('collapsed');
  page.classList.toggle('sidebar-collapsed');
  
  // Update button text and position
  if (sidebar.classList.contains('collapsed')) {{
    toggleBtn.innerHTML = '▶';
    toggleBtn.classList.add('sidebar-collapsed');
  }} else {{
    toggleBtn.innerHTML = '◀';
    toggleBtn.classList.remove('sidebar-collapsed');
  }}
}}

// Initialize on load
updateSelection();
</script>
</body>
</html>
"""


def derive_temp_output_path(repo_url: str) -> pathlib.Path:
    """Derive a temporary output path from the repo URL."""
    parts = repo_url.rstrip('/').split('/')
    if len(parts) >= 2:
        repo_name = parts[-1]
        if repo_name.endswith('.git'):
            repo_name = repo_name[:-4]
        filename = f"{repo_name}.html"
    else:
        filename = "repo.html"
    return pathlib.Path(tempfile.gettempdir()) / filename


def main() -> int:
    ap = argparse.ArgumentParser(description="Interactive GitHub repo viewer with selectable files")
    ap.add_argument("repo_url", help="GitHub repo URL (https://github.com/owner/repo[.git])")
    ap.add_argument("-o", "--out", help="Output HTML file path (default: temporary file)")
    ap.add_argument("--max-bytes", type=int, default=MAX_DEFAULT_BYTES, help="Max file size to render (default: 50KB)")
    ap.add_argument("--no-open", action="store_true", help="Don't open HTML in browser")
    ap.add_argument("--keep-bloat", action="store_true", help="Don't auto-skip bloat files (lock files, node_modules, etc.)")
    args = ap.parse_args()

    if args.out is None:
        args.out = str(derive_temp_output_path(args.repo_url))

    tmpdir = tempfile.mkdtemp(prefix="rendergit_lite_")
    repo_dir = pathlib.Path(tmpdir, "repo")

    try:
        print(f"📁 Cloning {args.repo_url}...", file=sys.stderr)
        git_clone(args.repo_url, str(repo_dir))
        head = git_head_commit(str(repo_dir))
        print(f"✓ Clone complete (HEAD: {head[:8]})", file=sys.stderr)

        print(f"📊 Scanning files...", file=sys.stderr)
        skip_bloat = not args.keep_bloat
        infos = collect_files(repo_dir, args.max_bytes, skip_bloat)
        rendered_count = sum(1 for i in infos if i.decision.include)
        skipped_count = len(infos) - rendered_count
        print(f"✓ Found {len(infos)} files ({rendered_count} rendered, {skipped_count} skipped)", file=sys.stderr)

        print(f"🔨 Generating interactive HTML...", file=sys.stderr)
        html_out = build_html(args.repo_url, repo_dir, head, infos)

        out_path = pathlib.Path(args.out)
        print(f"💾 Writing to {out_path}...", file=sys.stderr)
        out_path.write_text(html_out, encoding="utf-8")
        print(f"✓ Wrote {bytes_human(out_path.stat().st_size)}", file=sys.stderr)

        if not args.no_open:
            print(f"🌐 Opening in browser...", file=sys.stderr)
            webbrowser.open(f"file://{out_path.resolve()}")

        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()

