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

Distance is ROAD distance (routing.py): the road km and travel time from the
patient to every hospital come from an OSRM routing server in one call. Without
internet it falls back to an estimate (straight line x 1.35); each result says
which source was used.
The ranking is a SNAPSHOT: availability can change before the reservation
commits, which is why the transaction manager re-checks under a lock.
"""
import config
import db
import routing
from routing import haversine_km   # re-exported (used by tests and older code)


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
               (SELECT a.available_units FROM resources a
                 WHERE a.hospital_id = h.hospital_id AND a.resource_type = 'AMBULANCE') AS ambulances_free,
               (SELECT SUM(total_units) FROM resources x WHERE x.hospital_id = h.hospital_id
                   AND x.resource_type <> 'AMBULANCE') AS all_total,
               (SELECT SUM(available_units) FROM resources x WHERE x.hospital_id = h.hospital_id
                   AND x.resource_type <> 'AMBULANCE') AS all_free
          FROM hospitals h
          JOIN resources r ON r.hospital_id = h.hospital_id AND r.resource_type = %s
         WHERE h.is_lab = 0
        """,
        (specialist, resource_type),
    )


def score_hospitals(lat, lon, specialist, candidates, weights=None, distances=None):
    """
    Pure scoring function (no database, no network), so it can be unit tested.
    `candidates` are dicts like the rows returned by fetch_candidates().
    `distances`  optional {hospital_id: {"km", "minutes", "source"}} with ROAD
                 distances; hospitals missing from it use the offline estimate.
    Hospitals with zero free units of the required resource are dropped, and so are
    hospitals whose ambulances are all out (when the row says so: `ambulances_free`).
    """
    weights = weights or config.RANK_WEIGHTS
    distances = distances or {}
    usable = [dict(c) for c in candidates if _can_take(c)]
    if not usable:
        return []

    for c in usable:
        d = distances.get(c["hospital_id"])
        if d is None:
            km, mins = routing.estimate(lat, lon, c["latitude"], c["longitude"])
            d = {"km": km, "minutes": mins, "source": "ESTIMATE"}
        c["distance_km"] = d["km"]
        c["travel_min"] = round(d["minutes"], 1)
        c["distance_source"] = d["source"]
        c["straight_km"] = round(haversine_km(lat, lon, c["latitude"], c["longitude"]), 2)

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


def _can_take(c):
    """A free unit of the right type, and an ambulance to bring the patient in."""
    amb = c.get("ambulances_free", 1)
    return int(c["available_units"]) > 0 and amb is not None and int(amb) > 0


def road_distances_to(lat, lon, hospitals):
    """{hospital_id: {km, minutes, source}} for the given hospital rows (one routing call)."""
    found = routing.road_distances(lat, lon, [(h["latitude"], h["longitude"]) for h in hospitals])
    return {h["hospital_id"]: d for h, d in zip(hospitals, found)}


def rank_for_request(request):
    """Rank hospitals for one SOS request row (needs lat/lon, resource, specialist)."""
    candidates = fetch_candidates(request["required_resource"], request["required_specialist"])
    usable = [c for c in candidates if _can_take(c)]
    distances = road_distances_to(request["latitude"], request["longitude"], usable)
    return score_hospitals(request["latitude"], request["longitude"],
                           request["required_specialist"], candidates, distances=distances)


def nearest_hospital(lat, lon):
    """Nearest hospital by road (no capacity check) - shown next to the ranked choice."""
    rows = db.query_all("SELECT hospital_id, name, phone, latitude, longitude FROM hospitals WHERE is_lab = 0")
    if not rows:
        return None
    dist = road_distances_to(lat, lon, rows)
    best = min(rows, key=lambda h: dist[h["hospital_id"]]["km"])
    d = dist[best["hospital_id"]]
    return {"hospital_id": best["hospital_id"], "name": best["name"], "phone": best["phone"],
            "distance_km": round(d["km"], 2), "travel_min": round(d["minutes"], 1),
            "distance_source": d["source"]}
