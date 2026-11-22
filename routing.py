"""
routing.py - road distance, travel time and route lines (Phase III).

Straight-line (Haversine) distance is not how an ambulance travels: a hospital
3 km away across a river can be 9 km by road. So the ranking module uses ROAD
distance from an OSRM routing server (open-source router on OpenStreetMap data):

    Table service : one call gives road km + minutes from the patient to EVERY hospital
    Route service : the road path (list of points) drawn on the map

If the routing server cannot be reached (no internet in the lab, timeout) the
system keeps working: it falls back to an ESTIMATE

    road_km  = straight_line_km x ROAD_FACTOR   (1.35, a typical city detour ratio)
    minutes  = road_km / AVG_SPEED_KMPH x 60

and every result says which one was used (source = "ROAD" or "ESTIMATE").
After a failure the server is not asked again for BACKOFF_SEC, so one network
problem does not slow down every dispatch. Results are cached in memory.

Uses only the Python standard library (urllib), no extra packages.
"""
import json
import math
import threading
import time
import urllib.error
import urllib.request

import config

BACKOFF_SEC = 60
_CACHE_MAX = 2000
_cache = {}                       # key -> value  (simple bounded cache)
_lock = threading.Lock()
_down_until = 0.0                 # routing server considered down until this time
_last_error = None


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle (straight-line) distance between two points in kilometres."""
    r = 6371.0
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp = p2 - p1
    dl = math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def estimate(lat1, lon1, lat2, lon2):
    """Offline fallback: (road_km, minutes) estimated from the straight line."""
    km = haversine_km(lat1, lon1, lat2, lon2) * config.ROAD_FACTOR
    return km, km / config.AVG_SPEED_KMPH * 60


def _key(*parts):
    return tuple(round(float(p), 5) for p in parts)


def _cache_get(key):
    with _lock:
        return _cache.get(key)


def _cache_put(key, value):
    with _lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[key] = value


def clear_cache():
    global _down_until, _last_error
    with _lock:
        _cache.clear()
    _down_until, _last_error = 0.0, None


def _enabled():
    return config.ROUTING_MODE == "osrm" and time.time() >= _down_until


def _mark_down(err):
    global _down_until, _last_error
    _down_until = time.time() + BACKOFF_SEC
    _last_error = str(err)


def _get_json(url):
    """HTTP GET -> parsed JSON. Separate function so tests can replace it."""
    req = urllib.request.Request(url, headers={"User-Agent": "EmergencySOS-student-project"})
    with urllib.request.urlopen(req, timeout=config.ROUTING_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


def status():
    """For the UI: which distance source is active right now."""
    if config.ROUTING_MODE != "osrm":
        return {"mode": "ESTIMATE", "reason": "routing switched off (ROUTING_MODE=off)"}
    if time.time() < _down_until:
        return {"mode": "ESTIMATE", "reason": f"routing server not reachable: {_last_error}"}
    return {"mode": "ROAD", "server": config.OSRM_URL}


def road_distances(lat, lon, points):
    """
    Road distance from (lat, lon) to each point.
    points : list of (lat, lon)
    returns: list of dicts {km, minutes, source} in the same order.
    """
    out = [None] * len(points)
    missing = []
    for i, (plat, plon) in enumerate(points):
        hit = _cache_get(("d",) + _key(lat, lon, plat, plon))
        if hit:
            out[i] = hit
        else:
            missing.append(i)

    if missing and _enabled():
        coords = ";".join([f"{float(lon):.6f},{float(lat):.6f}"] +
                          [f"{float(points[i][1]):.6f},{float(points[i][0]):.6f}" for i in missing])
        url = (f"{config.OSRM_URL}/table/v1/driving/{coords}"
               f"?sources=0&annotations=distance,duration")
        try:
            data = _get_json(url)
            if data.get("code") != "Ok":
                raise ValueError(data.get("message") or data.get("code"))
            dists, durs = data["distances"][0], data["durations"][0]
            for j, i in enumerate(missing, start=1):
                if dists[j] is None or durs[j] is None:     # no road found to this point
                    continue
                val = {"km": dists[j] / 1000.0, "minutes": durs[j] / 60.0, "source": "ROAD"}
                _cache_put(("d",) + _key(lat, lon, *points[i]), val)
                out[i] = val
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError) as e:
            _mark_down(e)

    for i, (plat, plon) in enumerate(points):     # anything still missing -> estimate
        if out[i] is None:
            km, mins = estimate(lat, lon, plat, plon)
            out[i] = {"km": km, "minutes": mins, "source": "ESTIMATE"}
    return out


def route(lat1, lon1, lat2, lon2):
    """
    Road path for the map: {km, minutes, source, path: [[lat, lon], ...]}.
    Falls back to a straight line (2 points) when the routing server is unavailable.
    """
    key = ("r",) + _key(lat1, lon1, lat2, lon2)
    hit = _cache_get(key)
    if hit:
        return hit
    if _enabled():
        url = (f"{config.OSRM_URL}/route/v1/driving/"
               f"{float(lon1):.6f},{float(lat1):.6f};{float(lon2):.6f},{float(lat2):.6f}"
               f"?overview=full&geometries=geojson")
        try:
            data = _get_json(url)
            if data.get("code") != "Ok" or not data.get("routes"):
                raise ValueError(data.get("message") or data.get("code"))
            r = data["routes"][0]
            val = {"km": round(r["distance"] / 1000.0, 2), "minutes": round(r["duration"] / 60.0, 1),
                   "source": "ROAD",
                   "path": [[c[1], c[0]] for c in r["geometry"]["coordinates"]]}   # GeoJSON is lon,lat
            _cache_put(key, val)
            return val
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError) as e:
            _mark_down(e)
    km, mins = estimate(lat1, lon1, lat2, lon2)
    return {"km": round(km, 2), "minutes": round(mins, 1), "source": "ESTIMATE",
            "path": [[float(lat1), float(lon1)], [float(lat2), float(lon2)]]}
