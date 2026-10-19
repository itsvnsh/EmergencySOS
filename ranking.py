"""
ranking.py - Hospital Ranking Module.

Scores every hospital that currently has a free unit of the resource the
patient needs, using a weighted formula (weights live in config.py):

    score = 100 * ( 0.40 * distance_score      closer is better
                  + 0.25 * units_score         more free units is better
                  + 0.20 * specialist_score    right doctor on shift
                  + 0.15 * load_score )        less crowded hospital is better

Each part is scaled to 0..1:
  distance_score   = 1 - (d - d_min) / (d_max - d_min)      among the candidates
  units_score      = free_units / max free_units             among the candidates
  specialist_score = 1.0 required specialist on shift with spare capacity
                     0.5 only a general physician available
                     0.0 no suitable doctor on shift
  load_score       = 1 - occupancy  (occupancy = used units / total units, all types)

Distance is straight-line (Haversine). Road-distance routing is a pending task.
The ranking is a SNAPSHOT: availability can change before the reservation
commits, which is why the transaction manager re-checks under a lock.
"""
import math

import config
import db


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle (straight-line) distance between two points in kilometres."""
    r = 6371.0
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp = p2 - p1
    dl = math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fetch_candidates(resource_type, specialist):
    """Read the current availability snapshot for every (non-lab) hospital."""
    return db.query_all(
        """
        SELECT h.hospital_id, h.name, h.area, h.phone, h.latitude, h.longitude,
               r.resource_id, r.available_units, r.total_units,
               (SELECT COUNT(*) FROM doctors d
                 WHERE d.hospital_id = h.hospital_id AND d.specialization = %s
                   AND d.on_shift = 1 AND d.current_cases < d.max_cases) AS specialists_free,
               (SELECT COUNT(*) FROM doctors d
                 WHERE d.hospital_id = h.hospital_id AND d.specialization = 'GENERAL_PHYSICIAN'
                   AND d.on_shift = 1 AND d.current_cases < d.max_cases) AS physicians_free,
               (SELECT SUM(total_units) FROM resources x WHERE x.hospital_id = h.hospital_id) AS all_total,
               (SELECT SUM(available_units) FROM resources x WHERE x.hospital_id = h.hospital_id) AS all_free
          FROM hospitals h
          JOIN resources r ON r.hospital_id = h.hospital_id AND r.resource_type = %s
         WHERE h.is_lab = 0
        """,
        (specialist, resource_type),
    )


def score_hospitals(lat, lon, specialist, candidates, weights=None):
    """
    Pure scoring function (no database), so it can be unit tested.
    `candidates` are dicts like the rows returned by fetch_candidates().
    Hospitals with zero free units of the required resource are dropped.
    """
    weights = weights or config.RANK_WEIGHTS
    usable = [dict(c) for c in candidates if int(c["available_units"]) > 0]
    if not usable:
        return []

    for c in usable:
        c["distance_km"] = haversine_km(lat, lon, c["latitude"], c["longitude"])

    d_min = min(c["distance_km"] for c in usable)
    d_max = max(c["distance_km"] for c in usable)
    u_max = max(int(c["available_units"]) for c in usable)

    for c in usable:
        dist = 1.0 if d_max == d_min else 1 - (c["distance_km"] - d_min) / (d_max - d_min)
        units = int(c["available_units"]) / u_max
        if int(c.get("specialists_free") or 0) > 0:
            spec = 1.0
        elif specialist != "GENERAL_PHYSICIAN" and int(c.get("physicians_free") or 0) > 0:
            spec = 0.5
        else:
            spec = 0.0
        total = float(c.get("all_total") or 0)
        free = float(c.get("all_free") or 0)
        occupancy = (total - free) / total if total else 1.0
        load = 1 - occupancy

        c["parts"] = {"distance": round(dist, 3), "units": round(units, 3),
                      "specialist": spec, "load": round(load, 3)}
        c["score"] = round(100 * (weights["distance"] * dist + weights["units"] * units
                                  + weights["specialist"] * spec + weights["load"] * load), 2)
        c["distance_km"] = round(c["distance_km"], 2)
        c["occupancy_pct"] = round(occupancy * 100, 1)
        c["latitude"] = float(c["latitude"])
        c["longitude"] = float(c["longitude"])

    # best score first; nearer hospital wins a tie
    usable.sort(key=lambda c: (-c["score"], c["distance_km"]))
    for rank, c in enumerate(usable, start=1):
        c["rank"] = rank
    return usable


def rank_for_request(request):
    """Rank hospitals for one SOS request row (needs lat/lon, resource, specialist)."""
    candidates = fetch_candidates(request["required_resource"], request["required_specialist"])
    return score_hospitals(request["latitude"], request["longitude"],
                           request["required_specialist"], candidates)


def nearest_hospital(lat, lon):
    """Plain nearest hospital (no capacity check) - shown next to the ranked choice."""
    rows = db.query_all("SELECT hospital_id, name, phone, latitude, longitude FROM hospitals WHERE is_lab = 0")
    if not rows:
        return None
    best = min(rows, key=lambda h: haversine_km(lat, lon, h["latitude"], h["longitude"]))
    return {"hospital_id": best["hospital_id"], "name": best["name"], "phone": best["phone"],
            "distance_km": round(haversine_km(lat, lon, best["latitude"], best["longitude"]), 2)}
