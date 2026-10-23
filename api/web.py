"""The built React dashboard (web/dist), served by the API at / so GateSense is one process on one port.

- /assets/* (Vite's hashed files) are served with a long cache; index.html never is, so a new build shows at once.
- Any other path that isn't /api/... (or the API docs) gets index.html: the React router shows the page
  (/attendance, /people/3, ...). Unknown /api/... paths stay JSON 404s, and so do paths that could reach outside
  the build (absolute, drive letters, UNC, backslashes, ..): refused before the file system is touched.
- Without a build, / explains how to make one (503).

build_status() and build() are for start.bat (python -m api.run --preflight): rebuild when web/ is newer than the
build, if Node.js is installed.
"""

import shutil
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from starlette.staticfiles import StaticFiles

from app import config

NOT_BUILT = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>GateSense</title></head>
<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem;line-height:1.5">
<h1>GateSense: the web dashboard isn't built yet</h1>
<p>In the project folder run <code>cd web</code>, <code>npm install</code> and <code>npm run build</code>, then reload
this page (or start GateSense with <code>start.bat</code>, which does this when Node.js is installed).</p>
<p>The API itself is running: <a href="/docs">/docs</a>, <a href="/api/health">/api/health</a>.</p></body></html>"""


class CachedAssets(StaticFiles):
    """Vite's hashed files never change: let the browser keep them."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def safe_web_path(path: str) -> bool:
    """Can `path` (from the URL) name a file inside the build? Checked on the text, before the file system is
    touched: on Windows, joining "//host/share/x", "\\\\host\\share" or "C:/x" to the build folder gives a path
    outside it, and resolving a UNC path already opens a network connection to that host (and sends this
    account's credentials to it). So: no absolute paths, drive letters or other colons, backslashes or "..".
    """
    if not path or "\\" in path or ":" in path or "\0" in path or path.startswith("/"):
        return False
    posix, windows = PurePosixPath(path), PureWindowsPath(path)
    return not (posix.is_absolute() or windows.drive or windows.root or ".." in posix.parts)


def mount(app: FastAPI, dist: Path | None = None) -> None:
    """Serve the build. Call after every API router: the catch-all route must come last."""
    dist = Path(dist or config.WEB_DIST)
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", CachedAssets(directory=assets), name="assets")
    root = dist.resolve()
    index = root / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    def web_app(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "Not Found")
        if path and not safe_web_path(path):
            raise HTTPException(404, "Not Found")  # never resolved: see safe_web_path
        if not index.is_file():
            return HTMLResponse(NOT_BUILT, status_code=503)
        if path:
            candidate = (root / path).resolve()
            if root in candidate.parents and candidate.is_file():  # favicon.svg and other top-level files
                return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


# --- Build check (start.bat) ----------------------------------------------------------------------------

SOURCES = ("src", "public", "index.html", "package.json", "package-lock.json", "vite.config.ts", "tsconfig.json",
           "tsconfig.app.json")


def newest_source(web: Path) -> float:
    times = []
    for name in SOURCES:
        path = web / name
        if path.is_file():
            times.append(path.stat().st_mtime)
        elif path.is_dir():
            times += [f.stat().st_mtime for f in path.rglob("*") if f.is_file()]
    return max(times, default=0.0)


def build_status(web: Path | None = None, dist: Path | None = None) -> str:
    """"ok", "stale" (web/ changed since the build) or "missing"."""
    web = Path(web or config.ROOT_DIR / "web")
    index = Path(dist or config.WEB_DIST) / "index.html"
    if not index.is_file():
        return "missing"
    return "stale" if newest_source(web) > index.stat().st_mtime else "ok"


def build(web: Path | None = None, log=print) -> bool:
    """npm ci (first time) and npm run build; False if Node.js isn't installed or the build failed."""
    web = Path(web or config.ROOT_DIR / "web")
    npm = shutil.which("npm")
    if npm is None:
        log("Node.js (npm) is not installed, so the web dashboard can't be built here.")
        return False
    steps = ([[npm, "ci", "--no-audit", "--no-fund"]] if not (web / "node_modules").is_dir() else []) + [
        [npm, "run", "build"]]
    for cmd in steps:
        log(f"Running: npm {' '.join(cmd[1:])} (in web/) ...")
        if subprocess.run(cmd, cwd=web).returncode != 0:
            log(f"npm {' '.join(cmd[1:])} failed.")
            return False
    return True
