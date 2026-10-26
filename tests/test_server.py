"""The HTTP layer: auth, the page and PWA files, and a short walk over the API."""

import pytest
from fastapi.testclient import TestClient

from jev_walker.geo import point_along
from jev_walker.live import LiveWalk
from jev_walker.run import RunConfig
from jev_walker.server import create_app

from .test_live import Clock, Script

TOKEN = "s3cret"


@pytest.fixture
def client(grid_home, tmp_path):
    G, home = grid_home
    clock = Clock()
    walk = LiveWalk(G, home, Script(["east", "left", "left", "left", "left"]),
                    RunConfig(target_m=480, clean_lo_m=400, clean_hi_m=600),
                    area="test", log_dir=tmp_path, clock=clock)
    c = TestClient(create_app(walk, TOKEN))
    c.headers["X-Walk-Token"] = TOKEN
    c.walk, c.clock = walk, clock
    return c


def test_page_and_pwa_files_need_no_token(client):
    anon = TestClient(client.app)
    assert "Jev Walks" in anon.get("/").text
    assert anon.get("/manifest.webmanifest").json()["display"] == "standalone"
    assert anon.get("/sw.js").status_code == 200
    assert anon.get("/icon-192.png").headers["content-type"] == "image/png"
    assert anon.get("/icon-7.png").status_code == 404


def test_api_needs_the_token(client):
    anon = TestClient(client.app)
    assert anon.get("/api/view").status_code == 401
    assert anon.get("/api/view", headers={"X-Walk-Token": "nope"}).status_code == 401
    assert anon.get(f"/api/view?token={TOKEN}").status_code == 200


def test_walk_over_http(client):
    v = client.post("/api/start", json={"client_t": 1, "fix": {"lat": 41.958, "lon": -87.65, "accuracy": 5}}).json()
    assert v["phase"] == "walking" and v["decision"]["direction"] == "east"
    assert client.post("/api/start", json={}).status_code == 409        # one walk at a time
    for _ in range(5):
        trav = client.walk._target()
        for d in range(0, int(trav.length_m) + 20, 10):
            lon, lat = point_along(trav.coords, min(d, trav.length_m))
            client.clock.t += 7
            v = client.post("/api/gps", json={"fixes": [{"lat": lat, "lon": lon, "accuracy": 5}]}).json()
            if v["phase"] == "ended" or client.walk._target() is not trav:
                break
    assert v["phase"] == "ended" and v["end"]["score"]["clean_finish"]
    log = client.get("/api/log")
    assert log.status_code == 200 and "attachment" in log.headers["content-disposition"]
    assert len(log.json()["decisions"]) == 6


def test_override_errors_are_409(client):
    client.post("/api/start", json={})
    r = client.post("/api/override", json={"option_id": "Z", "reason": "other"})
    assert r.status_code == 409
    assert client.post("/api/retry", json={}).status_code == 409
    assert client.post("/api/start", json={"mode": "sideways"}).status_code == 422
