#!/usr/bin/env python3
"""Minimal HTTP server for Session Viewer.

Serves session-viewer.html and provides API to browse JSONL files
from ~/.claude/projects/.

Usage: python3 serve.py
"""

import os
import re
import json
import time
import functools
from datetime import datetime, timezone
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session-viewer.html")
PORT = 8124


@functools.lru_cache(maxsize=512)
def decode_project_name(dir_name):
    """Convert encoded dir name back to a human-readable project path.

    Claude encodes project paths by replacing '/' with '-'. However, real
    hyphens in directory names (e.g. 'team-workspace') are indistinguishable
    from path separators in the encoded string. We resolve this by greedily
    matching against the real filesystem — combining segments with '-' until
    an actual directory is found.
    """
    stripped = dir_name.lstrip("-")
    parts = stripped.split("-")
    if len(parts) <= 2:
        return dir_name

    # Walk from /, longest-match-first: try combining all remaining segments
    # first, then shorten until a real directory is found.
    current = ""
    i = 0
    result_parts = []
    while i < len(parts):
        matched = False
        # Try longest candidate first: parts[i..end], then shorten
        for end in range(len(parts), i, -1):
            candidate = "-".join(parts[i:end])
            test_path = current + "/" + candidate if current else "/" + candidate
            if os.path.isdir(test_path):
                result_parts.append(candidate)
                current = test_path
                i = end
                matched = True
                break
        if not matched:
            result_parts.append(parts[i])
            current = current + "/" + parts[i] if current else "/" + parts[i]
            i += 1

    # Return everything after the home directory prefix
    home_parts = os.path.expanduser("~").strip("/").split("/")
    strip_count = len(home_parts)
    return "/".join(result_parts[strip_count:]) if len(result_parts) > strip_count else dir_name


def _try_parse_title(lines):
    """Try to extract a title from a list of JSONL lines. Returns title or ''."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if rec.get("type") != "user":
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    text = block["text"]
                    break
        if text:
            text = re.sub(r'<[^>]+>', '', text).strip()
            return text[:120].replace("\n", " ").strip()
    return ""


def extract_title(full_path):
    """Read the first user message from a JSONL file to use as title.

    Uses progressive head-reading: reads in 4KB chunks up to 64KB max,
    avoiding reading entire large files when the title is near the top.
    """
    try:
        chunk_size = 4096
        max_read = 65536  # 64KB
        tail = ""  # incomplete line from previous chunk
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            total_read = 0
            while total_read < max_read:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                total_read += len(chunk)
                text = tail + chunk
                lines = text.split("\n")
                # Last element may be incomplete — save for next iteration
                tail = lines.pop()
                result = _try_parse_title(lines)
                if result:
                    return result
            # Process any remaining tail
            if tail.strip():
                result = _try_parse_title([tail])
                if result:
                    return result
    except OSError:
        pass
    return ""


def collect_jsonl_files():
    """Walk ~/.claude/projects/ and collect all .jsonl files with metadata."""
    files = []
    if not os.path.isdir(PROJECTS_DIR):
        return files
    for dirpath, dirnames, filenames in os.walk(PROJECTS_DIR):
        for fname in filenames:
            if not fname.endswith(".jsonl"):
                continue
            full_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(full_path, PROJECTS_DIR)
            parent_dir = rel_path.split("/")[0] if "/" in rel_path else ""
            project = decode_project_name(parent_dir) if parent_dir else ""
            try:
                st = os.stat(full_path)
            except OSError:
                continue
            title = extract_title(full_path)
            mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            # Extract parent session UUID and agent metadata for subagents
            parent_uuid = None
            agent_meta = None
            if '/subagents/' in rel_path:
                parent_uuid = rel_path.split('/subagents/')[0].split('/')[-1]
                meta_path = full_path.rsplit('.jsonl', 1)[0] + '.meta.json'
                if os.path.isfile(meta_path):
                    try:
                        with open(meta_path, 'r', encoding='utf-8') as mf:
                            agent_meta = json.load(mf)
                    except (json.JSONDecodeError, OSError):
                        pass
            files.append({
                "path": rel_path,
                "project": project,
                "title": title,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "mtime_iso": mtime_iso,
                "uuid": fname[:-6],  # strip .jsonl
                "parent_uuid": parent_uuid,
                "agent_meta": agent_meta,
            })
    return files


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default logging

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/" or path == "/session-viewer.html":
            self._serve_html()
        elif path == "/api/files":
            self._serve_file_list(params)
        elif path == "/file":
            self._serve_jsonl(params)
        else:
            self.send_error(404)

    def _serve_html(self):
        try:
            with open(HTML_FILE, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404, "session-viewer.html not found")

    def _serve_file_list(self, params):
        files = collect_jsonl_files()

        # Filter
        q = params.get("q", [""])[0].lower()
        if q:
            files = [f for f in files if q in f["project"].lower() or q in f["uuid"].lower() or q in f.get("title", "").lower()]

        # Sort
        sort_key = params.get("sort", ["mtime"])[0]
        if sort_key == "size":
            files.sort(key=lambda f: f["size"], reverse=True)
        elif sort_key == "project":
            files.sort(key=lambda f: f["project"])
        else:  # mtime default
            files.sort(key=lambda f: f["mtime"], reverse=True)

        # Limit
        limit = min(int(params.get("limit", ["100"])[0]), 500)
        files = files[:limit]

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(files).encode())

    def _serve_jsonl(self, params):
        rel_path = params.get("path", [""])[0]
        if not rel_path:
            self.send_error(400, "Missing path parameter")
            return

        # Security: resolve and ensure it stays within PROJECTS_DIR
        full_path = os.path.realpath(os.path.join(PROJECTS_DIR, rel_path))
        projects_real = os.path.realpath(PROJECTS_DIR)
        if not full_path.startswith(projects_real + os.sep) and full_path != projects_real:
            self.send_error(403, "Access denied")
            return
        if not full_path.endswith(".jsonl"):
            self.send_error(400, "Only .jsonl files allowed")
            return
        if not os.path.isfile(full_path):
            self.send_error(404, "File not found")
            return

        try:
            st = os.stat(full_path)
            with open(full_path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            self.send_error(500, "Error reading file")


def main():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Session Viewer: {url}")
    print(f"LAN access:     http://<your-lan-ip>:{PORT}")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
