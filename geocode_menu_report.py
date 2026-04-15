"""
geocode_menu_report.py  [v3 - fast async + visible errors]
----------------------------------------------------------
Geocodes the menu report CSV via ArcGIS (async, ~2-3 min for 2,800 rows),
then spatially joins each point to its Cook County Census Block Group.

Requirements:
    pip install pandas geopandas aiohttp tqdm pygris

Usage:
    python geocode_menu_report.py --input quarterly_menu_report_Q4_2024_expanded.csv
"""

import asyncio
import argparse
import time
from pathlib import Path

import aiohttp
import pandas as pd
import geopandas as gpd
from tqdm import tqdm

# ── CONFIG ────────────────────────────────────────────────────────────────────
DEFAULT_INPUT  = "quarterly_menu_report_Q4_2024_expanded.csv"
DEFAULT_OUTPUT = "menu_report_geocoded.csv"

ARCGIS_URL   = (
    "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer"
    "/findAddressCandidates"
)
CONCURRENCY  = 20    # simultaneous requests — ArcGIS handles this fine
MAX_RETRIES  = 3


# ── ASYNC GEOCODER ────────────────────────────────────────────────────────────

async def geocode_test(address):
    """Hit ArcGIS with one address and print the raw response — for debugging."""
    params = {
        "SingleLine":   address,
        "f":            "json",
        "outFields":    "Match_addr",
        "maxLocations": 1,
    }
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(ARCGIS_URL, params=params,
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            print(f"\n  HTTP status : {resp.status}")
            raw = await resp.text()
            print(f"  Raw response: {raw[:1000]}")


async def geocode_one(session, sem, row_id, address):
    """Returns (row_id, lat, lon, error_msg)."""
    params = {
        "SingleLine":  address,
        "f":           "json",
        "outFields":   "Match_addr",
        "maxLocations": 1,
    }
    for attempt in range(MAX_RETRIES):
        try:
            async with sem:
                async with session.get(
                    ARCGIS_URL, params=params,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        err = f"HTTP {resp.status}"
                        if attempt == MAX_RETRIES - 1:
                            return row_id, None, None, err
                        await asyncio.sleep(1)
                        continue
                    data = await resp.json(content_type=None)
                    candidates = data.get("candidates", [])
                    if candidates:
                        loc = candidates[0]["location"]
                        return row_id, loc["y"], loc["x"], None
                    else:
                        return row_id, None, None, "no_result"
        except asyncio.TimeoutError:
            if attempt == MAX_RETRIES - 1:
                return row_id, None, None, "timeout"
            await asyncio.sleep(1)
        except aiohttp.ClientError as e:
            if attempt == MAX_RETRIES - 1:
                return row_id, None, None, f"connection_error: {e}"
            await asyncio.sleep(2)
        except Exception as e:
            return row_id, None, None, f"unexpected: {e}"
    return row_id, None, None, "max_retries_exceeded"


async def geocode_all_async(addr_map: dict):
    results = {}
    errors  = {}

    sem       = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY, ssl=False)

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            geocode_one(session, sem, row_id, addr)
            for row_id, addr in addr_map.items()
        ]
        pbar = tqdm(total=len(tasks), desc="Geocoding", unit="addr")
        for coro in asyncio.as_completed(tasks):
            row_id, lat, lon, err = await coro
            results[row_id] = (lat, lon)
            if err and err != "no_result":
                errors[row_id] = err
            pbar.update(1)
        pbar.close()

    # ── Report clearly ────────────────────────────────────────────────────
    matched   = sum(1 for v in results.values() if v[0] is not None)
    unmatched = sum(1 for v in results.values() if v[0] is None and results)

    print(f"\n  Geocoded:  {matched:,} matched")
    print(f"  No result: {unmatched - len(errors):,} (address not found by ArcGIS)")
    print(f"  Errors:    {len(errors):,}")

    if errors:
        sample = list(errors.items())[:5]
        print("\n  Sample errors:")
        for rid, msg in sample:
            print(f"    row {rid}: {msg}")

    if matched == 0:
        print("\n  ALL addresses failed to geocode.")
        print("  Most likely causes:")
        print("    1. No internet connection")
        print("    2. ArcGIS API is down or rate-limiting")
        print("  Test in your browser: https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer?f=json")

    return results


# ── COOK COUNTY BLOCK GROUPS ──────────────────────────────────────────────────

def load_cook_block_groups():
    cache = Path("cook_block_groups.gpkg")
    if cache.exists():
        print("  Loading block groups from local cache...")
        return gpd.read_file(cache)

    print("  Downloading Cook County block groups (pygris)...")
    try:
        from pygris import block_groups as pygris_bg
        # cb=True = simplified cartographic boundaries = much faster spatial join
        gdf = pygris_bg(state="IL", county="Cook", cb=True, year=2022, cache=True)
        gdf = gdf.to_crs(epsg=4326)
        gdf.to_file(cache, driver="GPKG")
        print(f"  Saved {len(gdf):,} block groups to cache: {cache}")
        return gdf
    except ImportError:
        raise SystemExit("pygris not installed. Run: pip install pygris")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input",  default=DEFAULT_INPUT)
    ap.add_argument("--output", default=DEFAULT_OUTPUT)
    ap.add_argument("--test",   action="store_true",
                    help="Test ArcGIS with one address and print the raw response")
    args = ap.parse_args()

    if args.test:
        sample = "2619 N Washtenaw Ave, Chicago, IL"
        print(f"Testing ArcGIS geocoder with: {sample}")
        asyncio.run(geocode_test(sample))
        return

    csv_path = Path(args.input)
    if not csv_path.exists():
        raise SystemExit(
            f"CSV not found: {csv_path}\n"
            "Place the CSV in the same folder as this script, "
            "or pass the full path with --input."
        )

    # 1. Load
    df = pd.read_csv(csv_path)
    df["row_id"] = df.index
    print(f"Loaded {len(df):,} rows from {csv_path.name}")

    df_valid = df[df["address"].notna() & (df["address"].str.strip() != "")].copy()

    # 2. Geocode (async) — skip if lat/lon already present in the input file
    if "lat" in df_valid.columns and "lon" in df_valid.columns:
        print("  lat/lon columns detected — skipping geocoding step.")
    else:
        df_valid["full_address"] = df_valid["address"] + ", Chicago, IL"
        print(f"  {len(df_valid):,} rows have addresses to geocode")
        print(f"\nGeocoding via ArcGIS (async, {CONCURRENCY} concurrent requests)...")
        t0       = time.time()
        addr_map = dict(zip(df_valid["row_id"], df_valid["full_address"]))
        geo      = asyncio.run(geocode_all_async(addr_map))
        print(f"  Elapsed: {time.time()-t0:.0f}s")

        df_valid["lat"] = df_valid["row_id"].map(lambda r: geo.get(r, (None, None))[0])
        df_valid["lon"] = df_valid["row_id"].map(lambda r: geo.get(r, (None, None))[1])

        # Save backup immediately — never lose geocoding work
        backup = Path(args.output).stem + "_geocoded_only.csv"
        df_valid.to_csv(backup, index=False)
        print(f"  Backup saved: {backup}")

    df_geo = df_valid[df_valid["lat"].notna()].copy()
    if df_geo.empty:
        raise SystemExit(
            "\nNo addresses geocoded — stopping before spatial join.\n"
            "Fix the geocoding issue above first."
        )

    # 3. Block groups
    print("\nLoading Cook County block groups...")
    cook_map = load_cook_block_groups()
    print(f"  {len(cook_map):,} block groups")

    # 4. Spatial join (fast — uses simplified cb=True boundaries)
    print("\nRunning spatial join...")
    t1  = time.time()
    gdf = gpd.GeoDataFrame(
        df_geo,
        geometry=gpd.points_from_xy(df_geo["lon"], df_geo["lat"]),
        crs="EPSG:4326"
    )
    keep   = [c for c in ["GEOID", "TRACTCE", "BLKGRPCE", "geometry"] if c in cook_map.columns]
    joined = gpd.sjoin(gdf, cook_map[keep], how="left", predicate="intersects")
    joined = joined[~joined.index.duplicated(keep="first")]
    joined = joined.rename(columns={
        "TRACTCE":  "Tract",
        "BLKGRPCE": "Block_Group",
        "GEOID":    "GEOID_Full",
    })
    print(f"  Spatial join done in {time.time()-t1:.1f}s")

    # 5. Save
    out_cols = ["ward", "menu_package", "address", "cost_numeric", "cost_share",
                "lat", "lon", "Tract", "Block_Group", "GEOID_Full"]
    out_cols = [c for c in out_cols if c in joined.columns]
    final    = joined[out_cols].sort_values(["ward", "menu_package"], na_position="last")
    final.to_csv(args.output, index=False)

    matched = final["GEOID_Full"].notna().sum()
    print(f"\nSaved {len(final):,} rows -> {args.output}")
    print(f"  Block group matched: {matched:,} / {len(final):,}")
    if matched < len(final):
        print(f"  Note: {len(final)-matched} points outside Cook County")
        print("        (vague addresses like intersections may geocode imprecisely)")
    print("\nDone.")


if __name__ == "__main__":
    main()
