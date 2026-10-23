"""Bygg 3D-bygningsfliser for Nordhordland.

Fotavtrykk kommer fra OpenStreetMap (Overpass-uttrekk). Høyden regnes ut fra
Kartverkets høydedata-API: overflatemodellen (DOM, laser 1 m) minus
terrengmodellen (DTM 1 m). Der lasermålingen ikke gir en troverdig høyde,
brukes OSM-taggene height / building:levels, og ellers et estimat per
bygningstype.

Bruk:
    python tools/build_buildings.py bygg.json hoyder_cache.json buildings/

Resultatet er Mapbox Vector Tiles i buildings/{z}/{x}/{y}.pbf (z13–15) og
buildings/stats.json.
"""
import json
import math
import os
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import mapbox_vector_tile
import requests
from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box, mapping
from shapely.ops import transform

API = "https://ws.geonorge.no/hoydedata/v1/datakilder/{src}/punkt"
UA = {"User-Agent": "nordhordland-3d-kart/1.0 (+https://github.com/sondreolsen/nordhordland-3d-kart)"}
ZOOMS = (13, 14, 15)

TYPE_DEFAULT = {
    "house": 7, "detached": 7, "residential": 7, "semidetached_house": 7,
    "terrace": 8, "apartments": 13, "farm": 7, "cabin": 5, "hut": 3.5,
    "garage": 3, "garages": 3, "carport": 2.5, "shed": 2.5, "boathouse": 4,
    "barn": 9, "farm_auxiliary": 6, "industrial": 9, "warehouse": 9,
    "commercial": 9, "retail": 7, "office": 12, "school": 10, "church": 14,
    "kindergarten": 5, "hospital": 15, "greenhouse": 4, "roof": 4,
}


# ---------- geometri ----------

def to_local(lat0):
    k = math.cos(math.radians(lat0)) * 111320.0

    def fwd(x, y, z=None):
        return (x * k, y * 110540.0)

    def inv(x, y, z=None):
        return (x / k, y / 110540.0)

    return fwd, inv


def ring(coords):
    pts = [(c["lon"], c["lat"]) for c in coords]
    if len(pts) < 4:
        return None
    return pts


def parse(path):
    data = json.load(open(path, encoding="utf-8"))
    out = []
    for e in data["elements"]:
        if e["type"] == "way" and "geometry" in e:
            r = ring(e["geometry"])
            if not r:
                continue
            poly = Polygon(r)
        elif e["type"] == "relation":
            outers = [ring(m["geometry"]) for m in e.get("members", []) if m.get("role") == "outer" and "geometry" in m]
            inners = [ring(m["geometry"]) for m in e.get("members", []) if m.get("role") == "inner" and "geometry" in m]
            outers = [o for o in outers if o]
            if not outers:
                continue
            polys = [Polygon(o, [i for i in inners if i and Polygon(o).contains(Polygon(i))]) for o in outers]
            poly = polys[0] if len(polys) == 1 else MultiPolygon(polys)
        else:
            continue
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty:
            continue
        out.append({"id": f"{e['type'][0]}{e['id']}", "tags": e.get("tags", {}), "geom": poly})
    return out


def sample_plan(b):
    """Velg målepunkt: fem punkt på tvers av mønet (kortaksen) + sentroiden for DTM."""
    g = b["geom"]
    c = g.representative_point()
    fwd, inv = to_local(c.y)
    gl = transform(fwd, g)
    b["area"] = gl.area
    obb = gl.minimum_rotated_rectangle
    if obb.geom_type != "Polygon":
        b["dom_pts"], b["dtm_pt"] = [(c.x, c.y)], (c.x, c.y)
        return
    xs = list(obb.exterior.coords)[:4]
    e1 = (xs[1][0] - xs[0][0], xs[1][1] - xs[0][1])
    e2 = (xs[2][0] - xs[1][0], xs[2][1] - xs[1][1])
    l1, l2 = math.hypot(*e1), math.hypot(*e2)
    long_v, short_v, L, W = (e1, e2, l1, l2) if l1 >= l2 else (e2, e1, l2, l1)
    b["obb"] = {"L": L, "W": W, "long": (long_v[0] / L, long_v[1] / L) if L else (1, 0),
                "short": (short_v[0] / W, short_v[1] / W) if W else (0, 1),
                "ratio": gl.area / obb.area if obb.area else 0}
    cl = transform(fwd, Point(c.x, c.y))
    oc = obb.centroid
    pts = []
    sx, sy = b["obb"]["short"]
    for t in (-0.36, -0.18, 0.0, 0.18, 0.36):
        p = Point(oc.x + sx * t * W, oc.y + sy * t * W)
        if gl.contains(p):
            pts.append(inv(p.x, p.y))
    if not pts:
        pts = [(c.x, c.y)]
    b["dom_pts"] = pts
    b["dtm_pt"] = inv(oc.x, oc.y) if gl.contains(oc) else (c.x, c.y)


# ---------- høydedata ----------

def key(p):
    return f"{p[0]:.6f},{p[1]:.6f}"


def fetch_batch(src, pts):
    q = json.dumps([[round(x, 6), round(y, 6)] for x, y in pts])
    url = API.format(src=src) + "?koordsys=4258&punkter=" + urllib.parse.quote(q)
    for attempt in range(5):
        try:
            r = requests.get(url, headers=UA, timeout=30)
            if r.status_code == 200:
                return [(key((p["x"], p["y"])), p.get("z")) for p in r.json()["punkter"]]
        except requests.RequestException:
            pass
        time.sleep(1.5 * (attempt + 1))
    return []


def fill_cache(cache, src, points):
    todo = sorted({key(p) for p in points} - set(cache[src]))
    print(f"{src}: {len(todo)} nye punkt å hente")
    batches = [todo[i:i + 50] for i in range(0, len(todo), 50)]
    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for res in ex.map(lambda bt: fetch_batch(src, [tuple(map(float, k.split(","))) for k in bt]), batches):
            for k, z in res:
                cache[src][k] = z
            done += 1
            if done % 200 == 0:
                print(f"  {src}: {done}/{len(batches)}")


# ---------- høyder ----------

def num(v):
    try:
        return float(str(v).replace(",", ".").replace("m", "").strip())
    except (TypeError, ValueError):
        return None


def heights(b, cache):
    t = b["tags"]
    ground = cache["dtm1"].get(key(b["dtm_pt"]))
    doms = [cache["dom1"].get(key(p)) for p in b["dom_pts"]]
    doms = [d for d in doms if d is not None]
    osm_h = num(t.get("height"))
    lv = num(t.get("building:levels"))
    osm_est = osm_h or (lv * 3.0 + 1.0 if lv else None)

    if ground is not None and doms:
        rel = [d - ground for d in doms]
        ridge = max(rel)
        edge = [rel[0], rel[-1]] if len(rel) >= 3 else rel
        eave = min(edge)
        plausible = 2.0 <= ridge <= 90 and eave >= 1.2
        # Trær over små bygg gir urimelige verdier: sammenlign med OSM der det finnes.
        if plausible and osm_est and ridge > osm_est * 2.5 + 4:
            plausible = False
        if plausible:
            return round(min(eave, ridge), 1), round(ridge, 1), "laser"
    if osm_est:
        return round(osm_est, 1), round(osm_est, 1), "osm"
    d = TYPE_DEFAULT.get(t.get("building", ""), 6)
    return float(d), float(d), "estimat"


def roof_steps(b, eave, ridge):
    """Tilnærm saltak med tre trinn langs mønet (smalere for hvert trinn)."""
    o = b.get("obb")
    if not o or ridge - eave < 1.2 or o["ratio"] < 0.8 or o["W"] < 4 or b["area"] > 2500:
        return []
    g = b["geom"]
    c = g.representative_point()
    fwd, inv = to_local(c.y)
    gl = transform(fwd, g)
    oc = gl.minimum_rotated_rectangle.centroid
    lx, ly = o["long"]
    half_l = o["L"] / 2 + 1
    ridge_line = LineString([(oc.x - lx * half_l, oc.y - ly * half_l), (oc.x + lx * half_l, oc.y + ly * half_l)])
    out = []
    for i in (1, 2, 3):
        width = o["W"] * (1 - i / 3.6)
        strip = ridge_line.buffer(width / 2, cap_style=2).intersection(gl)
        if strip.is_empty or strip.area < 1:
            continue
        base = eave + (ridge - eave) * (i - 1) / 3
        top = eave + (ridge - eave) * i / 3
        out.append((transform(inv, strip), round(base, 1), round(top, 1)))
    return out


# ---------- vektorfliser ----------

R = 6378137.0


def merc(x, y, z=None):
    return (math.radians(x) * R, math.log(math.tan(math.pi / 4 + math.radians(y) / 2)) * R)


def tile_bounds(z, x, y):
    n = 2 ** z
    size = 2 * math.pi * R / n
    minx = -math.pi * R + x * size
    maxy = math.pi * R - y * size
    return (minx, maxy - size, minx + size, maxy)


def tile_of(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def write_tiles(features, outdir):
    for z in ZOOMS:
        buckets = {}
        for f in features:
            if z == 13 and f["props"]["k"] == "r":
                continue
            minx, miny, maxx, maxy = f["geom"].bounds
            x0, y1 = tile_of(minx, miny, z)
            x1, y0 = tile_of(maxx, maxy, z)
            for tx in range(x0, x1 + 1):
                for ty in range(y0, y1 + 1):
                    buckets.setdefault((tx, ty), []).append(f)
        for (tx, ty), fs in buckets.items():
            b = tile_bounds(z, tx, ty)
            pad = (b[2] - b[0]) * 0.02
            clip = box(b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad)
            enc = []
            for f in fs:
                gm = transform(merc, f["geom"]).intersection(clip)
                if gm.is_empty or gm.geom_type not in ("Polygon", "MultiPolygon"):
                    continue
                enc.append({"geometry": gm.wkt, "properties": f["props"]})
            if not enc:
                continue
            data = mapbox_vector_tile.encode(
                [{"name": "buildings", "features": enc}],
                default_options={"quantize_bounds": b, "extents": 4096, "y_coord_down": False},
            )
            d = os.path.join(outdir, str(z), str(tx))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{ty}.pbf"), "wb") as fh:
                fh.write(data)
        print(f"z{z}: {len(buckets)} fliser")


def main(src, cache_path, outdir):
    blds = parse(src)
    print(f"{len(blds)} bygg lest")
    for b in blds:
        sample_plan(b)

    cache = {"dom1": {}, "dtm1": {}}
    if os.path.exists(cache_path):
        cache = json.load(open(cache_path))
    try:
        fill_cache(cache, "dtm1", [b["dtm_pt"] for b in blds])
        fill_cache(cache, "dom1", [p for b in blds for p in b["dom_pts"]])
    finally:
        json.dump(cache, open(cache_path, "w"))

    feats, stats = [], {"laser": 0, "osm": 0, "estimat": 0}
    for b in blds:
        eave, ridge, srcname = heights(b, cache)
        stats[srcname] += 1
        t = b["tags"]
        steps = roof_steps(b, eave, ridge) if srcname == "laser" else []
        top = eave if steps else ridge
        props = {"k": "w", "h": top, "b": 0, "r": ridge, "s": srcname, "id": b["id"],
                 "t": t.get("building", "yes")}
        if t.get("name"):
            props["n"] = t["name"][:60]
        feats.append({"geom": b["geom"], "props": props})
        for geom, base, h in steps:
            feats.append({"geom": geom, "props": {"k": "r", "h": h, "b": base, "s": srcname, "id": b["id"]}})

    os.makedirs(outdir, exist_ok=True)
    write_tiles(feats, outdir)
    stats["bygg"] = len(blds)
    stats["oppdatert"] = time.strftime("%Y-%m-%d")
    json.dump(stats, open(os.path.join(outdir, "stats.json"), "w"), ensure_ascii=False)
    print(stats)


if __name__ == "__main__":
    main(*sys.argv[1:4])
