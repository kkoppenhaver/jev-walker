"""Small spherical-geometry helpers. Everything is lon/lat in, metres/degrees out."""

from __future__ import annotations

import math

EARTH_R = 6_371_008.8

COMPASS_8 = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def bearing_deg(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, 0 = north, clockwise."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(x, y)) % 360


def wrap180(deg: float) -> float:
    """Normalise an angle to (-180, 180]."""
    d = (deg + 180) % 360 - 180
    return 180.0 if d == -180 else d


def compass_label(bearing: float) -> str:
    return COMPASS_8[int(((bearing % 360) + 22.5) // 45) % 8]


def point_along(coords: list[tuple[float, float]], dist_m: float) -> tuple[float, float]:
    """Point dist_m along a lon/lat polyline (clamped to its end)."""
    remaining = dist_m
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        seg = haversine_m(x1, y1, x2, y2)
        if seg >= remaining and seg > 0:
            f = remaining / seg
            return (x1 + (x2 - x1) * f, y1 + (y2 - y1) * f)
        remaining -= seg
    return coords[-1]


class LocalProjection:
    """Equirectangular projection around a reference point. Accurate to well under a
    metre across a neighbourhood, which is all the harness needs."""

    def __init__(self, lon0: float, lat0: float):
        self.lon0, self.lat0 = lon0, lat0
        self.kx = math.radians(1) * EARTH_R * math.cos(math.radians(lat0))
        self.ky = math.radians(1) * EARTH_R

    def to_xy(self, lon: float, lat: float) -> tuple[float, float]:
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)

    def to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        return (self.lon0 + x / self.kx, self.lat0 + y / self.ky)
