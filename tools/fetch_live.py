"""Hent sanntidsdata som ikke kan leses direkte fra nettleseren (mangler CORS).

- Politiloggen (Politiets fellestjenester): siste hendelser i Vest politidistrikt,
  stedfestet med Kartverkets stedsnavn-API.
- AIS (Kystverket): skipsposisjoner fra den åpne AIS-strømmen, filtrert til
  Nordhordland. Navn og skipstype slås sammen med forrige kjøring.

Kjøres av GitHub Actions hvert 10. minutt og skriver data/politi.json og data/ais.json.
"""
import json
import os
import socket
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
from pyais.stream import TCPConnection

UA = {"User-Agent": "nordhordland-3d-kart/1.0 (+https://github.com/sondreolsen/nordhordland-3d-kart)"}
SITE = "https://sondreolsen.github.io/nordhordland-3d-kart/"
BBOX = (4.4, 60.3, 6.3, 61.15)  # minLon, minLat, maxLon, maxLat
NORDHORDLAND = {"Alver", "Austrheim", "Fedje", "Masfjorden", "Modalen", "Osterøy", "Vaksdal"}
AIS_HOST, AIS_PORT = "153.44.253.27", 5631
AIS_SECONDS = int(os.environ.get("AIS_SECONDS", "150"))


def previous(name):
    try:
        r = requests.get(SITE + "data/" + name, headers=UA, timeout=20)
        if r.ok:
            return r.json()
    except (requests.RequestException, ValueError):
        pass
    return {}


# ---------- politiloggen ----------

def geocode(area, municipality, memo):
    k = f"{area}|{municipality}"
    if k in memo:
        return memo[k]
    pos = None
    for q in [area, municipality]:
        if not q:
            continue
        url = ("https://ws.geonorge.no/stedsnavn/v1/navn?treffPerSide=1&utkoordsys=4258&sok="
               + urllib.parse.quote(q) + ("&kommunenavn=" + urllib.parse.quote(municipality) if municipality else ""))
        try:
            hit = requests.get(url, headers=UA, timeout=20).json().get("navn", [])
        except (requests.RequestException, ValueError):
            hit = []
        if hit:
            p = hit[0]["representasjonspunkt"]
            pos = [round(p["øst"], 5), round(p["nord"], 5)]
            break
    memo[k] = pos
    return pos


def politi():
    msgs = []
    for skip in (0, 50):  # API-et gir maks 50 per side
        url = f"https://api.politiloggen.politiet.no/messages/?districts=Vest&take=50&skip={skip}"
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        page = r.json()
        msgs += page.get("messages", [])
        if not page.get("hasMoreResults"):
            break
    threads = {}
    for m in msgs:
        threads.setdefault(m["threadId"], []).append(m)
    memo = {(k): v for k, v in previous("politi.json").get("geocache", {}).items()}
    out = []
    for tid, ms in threads.items():
        ms.sort(key=lambda m: m["createdOn"])
        first, last = ms[0], ms[-1]
        pos = geocode(first.get("area") or "", first.get("municipality") or "", memo)
        out.append({
            "id": tid,
            "kategori": first.get("category"),
            "kommune": first.get("municipality"),
            "sted": first.get("area"),
            "aktiv": last.get("isActive"),
            "nordhordland": first.get("municipality") in NORDHORDLAND,
            "start": first["createdOn"],
            "oppdatert": last["updatedOn"],
            "meldinger": [{"tid": m["createdOn"], "tekst": m["text"]} for m in ms],
            "pos": pos,
        })
    out.sort(key=lambda t: t["oppdatert"], reverse=True)
    return {"hentet": datetime.now(timezone.utc).isoformat(), "hendelser": out,
            "geocache": {k: v for k, v in memo.items()}}


# ---------- AIS ----------

def in_box(lon, lat):
    return BBOX[0] <= lon <= BBOX[2] and BBOX[1] <= lat <= BBOX[3]


def ais():
    """AIS fra BarentsWatch hvis klient-ID er satt (fungerer fra GitHub), ellers Kystverkets TCP-strøm."""
    if os.environ.get("BW_CLIENT_ID") and os.environ.get("BW_CLIENT_SECRET"):
        return ais_barentswatch()
    return ais_tcp()


def ais_barentswatch():
    tok = requests.post("https://id.barentswatch.no/connect/token", timeout=30, headers=UA, data={
        "client_id": os.environ["BW_CLIENT_ID"], "client_secret": os.environ["BW_CLIENT_SECRET"],
        "scope": "ais", "grant_type": "client_credentials"})
    tok.raise_for_status()
    now = datetime.now(timezone.utc)
    x0, y0, x1, y1 = BBOX
    body = {
        "geometry": {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]},
        "since": (now - timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "modelType": "Full", "modelFormat": "Json",
    }
    r = requests.post("https://live.ais.barentswatch.no/live/v1/latest/combined", json=body, timeout=60,
                      headers={**UA, "Authorization": "Bearer " + tok.json()["access_token"]})
    r.raise_for_status()
    skip = []
    for s in r.json():
        lon, lat = s.get("longitude"), s.get("latitude")
        if lon is None or lat is None or not in_box(lon, lat):
            continue
        sog, cog, hdg = s.get("speedOverGround"), s.get("courseOverGround"), s.get("trueHeading")
        skip.append({
            "mmsi": str(s["mmsi"]), "lon": round(lon, 5), "lat": round(lat, 5),
            "fart": None if sog is None or sog >= 102.3 else round(sog, 1),
            "kurs": None if cog is None or cog >= 360 else round(cog, 1),
            "retning": None if hdg is None or hdg >= 511 else int(hdg),
            "tid": s.get("msgtime") or now.isoformat(),
            "navn": (s.get("name") or "").strip(), "type": s.get("shipType") or 0,
            "dest": (s.get("destination") or "").strip(), "lengde": s.get("shipLength") or 0,
        })
    print(f"AIS (BarentsWatch): {len(skip)} skip i området")
    return {"hentet": now.isoformat(), "kilde": "BarentsWatch", "skip": skip}


def ais_tcp():
    prev = previous("ais.json")
    names = prev.get("statisk", {})
    ships = {s["mmsi"]: s for s in prev.get("skip", [])}
    now = datetime.now(timezone.utc)
    socket.setdefaulttimeout(30)
    end = time.time() + AIS_SECONDS
    n = 0
    try:
        for raw in TCPConnection(AIS_HOST, port=AIS_PORT):
            if time.time() > end:
                break
            try:
                m = raw.decode()
            except Exception:
                continue
            n += 1
            mmsi = str(getattr(m, "mmsi", ""))
            t = m.msg_type
            if t in (1, 2, 3, 18, 19, 27):
                lon, lat = getattr(m, "lon", None), getattr(m, "lat", None)
                if lon is None or lat is None or not in_box(lon, lat):
                    continue
                sog = getattr(m, "speed", None)
                cog = getattr(m, "course", None)
                hdg = getattr(m, "heading", None)
                ships[mmsi] = {
                    "mmsi": mmsi, "lon": round(lon, 5), "lat": round(lat, 5),
                    "fart": None if sog is None or sog >= 102.3 else round(float(sog), 1),
                    "kurs": None if cog is None or cog >= 360 else round(float(cog), 1),
                    "retning": None if hdg is None or hdg >= 511 else int(hdg),
                    "tid": now.isoformat(),
                }
            elif t == 5:
                names[mmsi] = {"navn": (m.shipname or "").strip(), "type": int(m.ship_type or 0),
                               "dest": (m.destination or "").strip(), "lengde": (m.to_bow or 0) + (m.to_stern or 0)}
            elif t == 24 and getattr(m, "partno", None) == 0:
                names.setdefault(mmsi, {}).update({"navn": (m.shipname or "").strip()})
            elif t == 24:
                names.setdefault(mmsi, {}).update({"type": int(getattr(m, "ship_type", 0) or 0)})
    except (OSError, socket.timeout) as e:
        print("AIS-strøm avbrutt:", e, file=sys.stderr)

    cutoff = now - timedelta(minutes=45)
    keep = []
    for s in ships.values():
        if datetime.fromisoformat(s["tid"]) < cutoff:
            continue
        s.update({k: v for k, v in names.get(s["mmsi"], {}).items() if v not in (None, "")})
        keep.append(s)
    print(f"AIS: {n} meldinger lest, {len(keep)} skip i området")
    return {"hentet": now.isoformat(), "skip": keep,
            "statisk": {k: v for k, v in names.items() if k in {s['mmsi'] for s in keep} or len(names) < 5000}}


def main(outdir):
    os.makedirs(outdir, exist_ok=True)
    for name, fn in (("politi.json", politi), ("ais.json", ais)):
        try:
            data = fn()
        except Exception as e:  # behold forrige fil hvis kilden er nede
            print(f"{name}: feilet ({e}), beholder forrige versjon", file=sys.stderr)
            data = previous(name) or {"hentet": None}
        with open(os.path.join(outdir, name), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data")
