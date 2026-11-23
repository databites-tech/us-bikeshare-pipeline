"""Population of each system's service area. Run by hand about once a year.

  city_population_2025      Census Vintage 2025 estimate for the core city
  service_area_population   residents of census tracts within 400 m (about a 5-minute walk)
                            of a station used in the last 12 months (ACS 2020-2024 5-year, B01003)

Usage: pip install -r requirements-population.txt && python -m pipeline.population
"""
import subprocess

import geopandas as gpd
import pandas as pd

from .common import CITIES, WORK, read_table, write_table

WALK_M = 400
ACS = "https://www2.census.gov/programs-surveys/acs/summary_file/2024/table-based-SF/data/5YRData/acsdt5y2024-b01003.dat"
POPEST = "https://www2.census.gov/programs-surveys/popest/datasets/2020-2025/cities/totals/sub-est2025.csv"
TRACTS = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_{s}_tract_500k.zip"


def get(url: str):
    path = WORK / "census" / url.rsplit("/", 1)[1]
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        subprocess.run(["curl", "-s", "-f", "-m", "600", "-o", str(path), url], check=True)
    return path


def main():
    acs = pd.read_csv(get(ACS), sep="|", dtype=str)
    acs = acs[acs.GEO_ID.str.startswith("1400000US")]
    acs["GEOID"], acs["pop"] = acs.GEO_ID.str[9:], acs.B01003_E001.astype(int)
    pe = pd.read_csv(get(POPEST), dtype=str, encoding="latin-1")
    st = read_table("stations")
    out = []
    for city, cfg in CITIES.items():
        tracts = pd.concat([gpd.read_file(f"zip://{get(TRACTS.format(s=s))}") for s in cfg["states"]])
        tracts = tracts.to_crs(5070).merge(acs[["GEOID", "pop"]], on="GEOID", how="left")
        s = st[(st.city == city) & (st.trips_last_12m > 0)]
        pts = gpd.GeoSeries(gpd.points_from_xy(s.lon, s.lat), crs=4326).to_crs(5070)
        served = tracts[tracts.intersects(pts.buffer(WALK_M).union_all())]
        state, name = cfg["core_city"]
        core = pe[(pe.SUMLEV == "162") & (pe.STATE == state) & (pe.NAME == name)]
        out.append({"city": city, "city_name": cfg["name"],
                    "city_population_2025": int(core.POPESTIMATE2025.iloc[0]),
                    "service_area_population": int(served["pop"].sum()),
                    "service_area_tracts": len(served), "stations_used_last_12m": len(s)})
    df = pd.DataFrame(out)
    write_table("population", df)
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
