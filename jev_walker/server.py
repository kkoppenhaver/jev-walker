"""Backend for the phone: serves the page and wraps `LiveWalk` in a small JSON API.

    python -m jev_walker.server --area sf --chooser jev --token <secret>

The phone needs HTTPS for the Geolocation API, so put this behind a tunnel or a
host with TLS (see README). Every API call needs the token, sent as X-Walk-Token
or ?token=; the page picks it up once from the URL (?t=<secret>) and remembers it.
"""

from __future__ import annotations

import argparse
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .choosers import ChooserError, make_chooser
from .graph import AREAS, DEFAULT_AREA, ROOT, load_graph
from .live import OVERRIDE_REASONS, LiveWalk, WalkError
from .run import RunConfig

STATIC = Path(__file__).resolve().parent / "static"


class Fix(BaseModel):
    lat: float
    lon: float
    accuracy: float | None = None
    heading: float | None = None
    speed: float | None = None
    t: float | None = None          # phone epoch ms


class Base(BaseModel):
    client_t: float | None = None   # phone epoch ms at send


class StartReq(Base):
    fix: Fix | None = None
    mode: str | None = None


class GpsReq(Base):
    fixes: list[Fix]


class OverrideReq(Base):
    option_id: str
    reason: str


class EndReq(Base):
    reason: str = "manual"


def create_app(walk: LiveWalk, token: str | None) -> FastAPI:
    app = FastAPI(title="Jev Walks Me Home", docs_url=None, redoc_url=None)

    def auth(x_walk_token: str | None = Header(default=None), t: str | None = Query(default=None, alias="token")):
        if token and not secrets.compare_digest((x_walk_token or t or "").encode(), token.encode()):
            raise HTTPException(401, "bad or missing walk token")

    def guarded(fn):
        try:
            return fn()
        except WalkError as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    def sw():
        return FileResponse(STATIC / "sw.js", media_type="text/javascript")

    @app.get("/icon-{size}.png", include_in_schema=False)
    def icon(size: int):
        if size not in (192, 512):
            raise HTTPException(404)
        return FileResponse(STATIC / f"icon-{size}.png", media_type="image/png")

    @app.get("/api/view", dependencies=[Depends(auth)])
    def view():
        return walk.view() | {"reasons": OVERRIDE_REASONS}

    @app.post("/api/start", dependencies=[Depends(auth)])
    def start(req: StartReq):
        if req.mode not in (None, "compass", "pure"):
            raise HTTPException(422, "mode must be compass or pure")
        return guarded(lambda: walk.start(client_t=req.client_t, mode=req.mode,
                                          fix=req.fix.model_dump() if req.fix else None))

    @app.post("/api/gps", dependencies=[Depends(auth)])
    def gps(req: GpsReq):
        return walk.gps([f.model_dump() for f in req.fixes], client_t=req.client_t)

    @app.post("/api/arrive", dependencies=[Depends(auth)])
    def arrive(req: Base):
        return walk.arrive(client_t=req.client_t)

    @app.post("/api/override", dependencies=[Depends(auth)])
    def override(req: OverrideReq):
        return guarded(lambda: walk.override(req.option_id, req.reason, client_t=req.client_t))

    @app.post("/api/retry", dependencies=[Depends(auth)])
    def retry(req: Base):
        return guarded(lambda: walk.retry(client_t=req.client_t))

    @app.post("/api/sync", dependencies=[Depends(auth)])
    def sync(req: Base):
        return guarded(lambda: walk.sync(client_t=req.client_t))

    @app.post("/api/end", dependencies=[Depends(auth)])
    def end(req: EndReq):
        return walk.end(reason=req.reason, client_t=req.client_t)

    @app.get("/api/log", dependencies=[Depends(auth)])
    def log():
        data = walk.log()
        if data is None:
            raise HTTPException(404, "no walk yet")
        return JSONResponse(data, headers={
            "Content-Disposition": f'attachment; filename="{data["header"]["run_id"]}.json"'})

    return app


def build_walk(area: str, chooser: str, mode: str, paths: str, log_dir: Path) -> LiveWalk:
    G, home = load_graph(area=area, paths=paths)
    try:
        ch = make_chooser(chooser)
    except ChooserError as e:
        raise SystemExit(f"can't start the {chooser} chooser: {e}") from e
    cfg = RunConfig.for_area(area, mode=mode)
    return LiveWalk(G, home, ch, cfg, area=area, log_dir=log_dir)


def main(argv=None) -> int:
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--area", default=DEFAULT_AREA, choices=sorted(AREAS))
    ap.add_argument("--chooser", default="jev", choices=["jev", "claude", "heuristic", "random"])
    ap.add_argument("--mode", default="compass", choices=["compass", "pure"])
    ap.add_argument("--paths", default="named", choices=["named", "all"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--token", default=None, help="shared secret for the API (default: random)")
    ap.add_argument("--no-token", action="store_true", help="no auth (local testing only)")
    ap.add_argument("--log-dir", type=Path, default=ROOT / "runs" / "live")
    ap.add_argument("--resume", default=None, help="'latest' or a log path: pick up an unfinished walk")
    args = ap.parse_args(argv)

    token = None if args.no_token else (args.token or secrets.token_urlsafe(12))
    walk = build_walk(args.area, args.chooser, args.mode, args.paths, args.log_dir)
    if args.resume:
        path = Path(args.resume)
        if args.resume == "latest":
            logs = sorted(args.log_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
            if not logs:
                raise SystemExit(f"no logs in {args.log_dir}")
            path = logs[-1]
        print(f"resuming {path}: {walk.resume(path)['phase']}")
    h = walk.home
    print(f"area {args.area}, chooser {walk.chooser.name} ({walk.chooser.model_version}), mode {args.mode}")
    print(f"door node {h.node} at {h.lat:.5f},{h.lon:.5f} (snapped {h.snap_m:.0f} m)")
    print("open on the phone: https://<your-tunnel-host>/" + (f"?t={token}" if token else ""))
    uvicorn.run(create_app(walk, token), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
