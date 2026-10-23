# Nordhordland i 3D

Åpent 3D-kart over Nordhordland (Alver, Austrheim, Fedje, Masfjorden, Modalen, Osterøy og Vaksdal), bygget med [MapLibre GL JS](https://maplibre.org/).

**Live side:** https://sondreolsen.github.io/nordhordland-3d-kart/

## Innhold

| Lag | Kilde | Oppdatering |
|---|---|---|
| Bygningsomriss | OpenStreetMap (Overpass) | Forhåndsberegnet (`tools/build_buildings.py`) |
| Bygningshøyder | Kartverket høydedata-API: overflatemodell DOM 1 m (laser) minus terrengmodell DTM 1 m. Faller tilbake til OSM `height`/`building:levels`, ellers estimat per bygningstype | Forhåndsberegnet |
| Bakgrunnskart | OpenFreeMap (lyst kart), Kartverket WMTS (topo, gråtone, sjøkart), Esri flyfoto | Direkte |
| Terreng i 3D | AWS Terrain Tiles (inkluderer Kartverkets DTM) | Direkte |
| Busser, ferjer og tog | Entur sanntid (SIRI-VM, GraphQL) | Hvert 8. sekund, animert |
| Skip | Kystverkets åpne AIS-strøm | Hvert 10. minutt (GitHub Action) |
| Fyr og lykter | Kystverket, navigasjonsinstallasjoner (ArcGIS REST) | Direkte |
| Politiloggen | Politiets fellestjenester, stedfestet med Kartverkets stedsnavn-API | Hvert 10. minutt (GitHub Action) |
| Temperatur | MET Norge locationforecast | Ved flytting av kartet |

Politiloggen og AIS kan ikke leses direkte fra nettleseren (mangler CORS / er en TCP-strøm), derfor henter workflowen `.github/workflows/live.yml` dem hvert 10. minutt med `tools/fetch_live.py` og publiserer siden på nytt.

## Bygge bygningsflisene på nytt

```bash
pip install shapely mapbox-vector-tile requests numpy
# 1) Hent OSM-bygg for kommunene (Overpass) til bygg.json
# 2) Beregn høyder og skriv vektorfliser til buildings/{z}/{x}/{y}.pbf
python tools/build_buildings.py bygg.json hoyder_cache.json buildings
```

Saltak tilnærmes med tre trinn langs mønet når laserdataene viser mer enn 1,2 m forskjell mellom gesims og møne.

Data: © OpenStreetMap-bidragsytere (ODbL), © Kartverket, Entur AS, Kystverket, Politiets fellestjenester, MET Norge.
