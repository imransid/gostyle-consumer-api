#!/usr/bin/env python3
"""
Builds apps/salons/data/country_borders.json.gz, the country borders the map
picks a country from (apps/salons/countries.py, MAP_COUNTRY_V1).

The source is Natural Earth's 1:10m "Admin 0 - Countries", version 5.1.2:
public domain, free, no account and no key. Download it once, then run this:

    curl -sSL -o /tmp/ne_10m_admin_0_countries.geojson \\
      https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_10m_admin_0_countries.geojson
    python3 scripts/build_country_borders.py /tmp/ne_10m_admin_0_countries.geojson

Standard library only. The output is the same bytes for the same input, so a
rebuild that changes nothing shows no diff.

What it keeps: per country its ISO 3166-1 alpha-2 code (ISO_A2_EH, which also
fills France, Norway and Kosovo, where ISO_A2 says -99), its English name and
its polygons. Points are rounded to 5 decimals (about 1 m) and stored as
whole-number steps from the point before, which is what makes the file small.
"""

import argparse
import gzip
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "apps" / "salons" / "data" / "country_borders.json.gz"
SOURCE = "Natural Earth 1:10m Admin 0 - Countries, v5.1.2 (public domain)"
SCALE = 100_000

# Shapes Natural Earth gives no ISO code (ISO_A2_EH -99), filed under the
# country a customer standing there would book in. Every other -99 shape (a
# reef, a disputed glacier, Bir Tawil) is left out: a point there gets the
# nearest country within 25 km, or none.
CODE_FIXES = {
    "Somaliland": "SO",
    "Northern Cyprus": "CY",
    "Cyprus No Mans Area": "CY",
    "Dhekelia Sovereign Base Area": "CY",
    "Akrotiri Sovereign Base Area": "CY",
    "US Naval Base Guantanamo Bay": "CU",
}

# The app shows the name in "GoStyle isn't in <name> yet". Natural Earth's
# NAME_EN, except where it is a formal name nobody says.
NAME_FIXES = {
    "CN": "China",
    "US": "United States",
}


def ring_steps(ring):
    """A GeoJSON ring as [x0, y0, dx1, dy1, ...] in 1e-5 degree steps, or None."""
    points = []
    for lng, lat in ring:
        point = (round(lng * SCALE), round(lat * SCALE))
        if not points or point != points[-1]:
            points.append(point)
    # GeoJSON closes each ring on its first point; the reader closes it itself.
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    if len(points) < 3:
        return None
    steps = list(points[0])
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        steps += [x1 - x0, y1 - y0]
    return steps


def build(features):
    countries = {}
    for feature in features:
        props = feature["properties"]
        code = CODE_FIXES.get(props["ADMIN"], props["ISO_A2_EH"])
        if not code or code == "-99":
            continue
        geometry = feature["geometry"]
        shapes = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        polygons = []
        for shape in shapes:
            rings = [ring_steps(ring) for ring in shape]
            # The outer ring first; a polygon whose outer ring collapsed is gone.
            if rings and rings[0] is not None:
                polygons.append([ring for ring in rings if ring is not None])
        if not polygons:
            continue
        size = sum(len(ring) for polygon in polygons for ring in polygon)
        entry = countries.setdefault(code, {"code": code, "name": None, "size": -1, "polygons": []})
        entry["polygons"] += polygons
        # The name comes from the biggest shape: France, not Clipperton Island.
        if size > entry["size"]:
            entry["size"] = size
            entry["name"] = NAME_FIXES.get(code, props["NAME_EN"])

    return [
        {"code": entry["code"], "name": entry["name"], "polygons": entry["polygons"]}
        for entry in sorted(countries.values(), key=lambda entry: entry["code"])
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("geojson", type=Path, help="ne_10m_admin_0_countries.geojson")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    countries = build(json.loads(args.geojson.read_text(encoding="utf-8"))["features"])
    payload = {"source": SOURCE, "scale": SCALE, "countries": countries}
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0: the same input gives the same bytes.
    with open(args.out, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=9) as out:
        out.write(text)
    points = sum(len(ring) // 2 for country in countries for polygon in country["polygons"] for ring in polygon)
    print(f"{len(countries)} countries, {points} points, {args.out.stat().st_size} bytes -> {args.out}")


if __name__ == "__main__":
    main()
