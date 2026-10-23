"""Hent politiloggen, som ikke kan leses direkte fra nettleseren (mangler CORS).

Siste hendelser i Vest politidistrikt fra Politiets fellestjenester, stedfestet
med Kartverkets stedsnavn-API. Kjøres av GitHub Actions hvert 10. minutt og
skriver data/politi.json.
"""
import json
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests

UA = {"User-Agent": "nordhordland-3d-kart/1.0 (+https://github.com/sondreolsen/nordhordland-3d-kart)"}
SITE = "https://sondreolsen.github.io/nordhordland-3d-kart/"
NORDHORDLAND = {"Alver", "Austrheim", "Fedje", "Masfjorden", "Modalen", "Osterøy", "Vaksdal"}


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


def main(outdir):
    os.makedirs(outdir, exist_ok=True)
    for name, fn in (("politi.json", politi),):
        try:
            data = fn()
        except Exception as e:  # behold forrige fil hvis kilden er nede
            print(f"{name}: feilet ({e}), beholder forrige versjon", file=sys.stderr)
            data = previous(name) or {"hentet": None}
        with open(os.path.join(outdir, name), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data")
