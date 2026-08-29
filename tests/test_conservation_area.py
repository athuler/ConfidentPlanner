from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon

from Processing.conservation_area import (
    BRITISH_NATIONAL_GRID,
    classify_postcodes,
    enrich_dataframe,
    load_postcode_lookup,
    normalize_postcode,
)


def synthetic_areas():
    return gpd.GeoDataFrame(
        {"_conservation_area_name": ["Test Area"]},
        geometry=[
            Polygon(
                [
                    (529_900, 179_900),
                    (530_100, 179_900),
                    (530_100, 180_100),
                    (529_900, 180_100),
                ]
            )
        ],
        crs=BRITISH_NATIONAL_GRID,
    )


def synthetic_lookup():
    return pd.DataFrame(
        {
            "_postcode_key": ["AA11AA", "BB11BB"],
            "_easting": [530_000, 531_000],
            "_northing": [180_000, 181_000],
        }
    )


def test_normalize_postcode():
    assert normalize_postcode(" sw1a 1aa ") == "SW1A1AA"
    assert normalize_postcode(None) is None


def test_classify_inside_outside_and_unresolved_postcodes():
    postcodes = pd.Series(["AA1 1AA", "bb1 1bb", "ZZ9 9ZZ", None])

    result = classify_postcodes(postcodes, synthetic_areas(), synthetic_lookup())

    assert result["in_conservation_area"].tolist() == [1, 0, pd.NA, pd.NA]
    assert result.loc[0, "conservation_area_name"] == "Test Area"
    assert pd.isna(result.loc[1, "conservation_area_name"])


def test_enrich_dataframe_preserves_input_columns():
    applications = pd.DataFrame(
        {"reference": ["A", "B"], "site_postcode": ["AA1 1AA", "BB1 1BB"]}
    )

    result = enrich_dataframe(
        applications,
        synthetic_areas(),
        synthetic_lookup(),
        postcode_column="site_postcode",
    )

    assert result["reference"].tolist() == ["A", "B"]
    assert result["in_conservation_area"].tolist() == [1, 0]


def test_load_postcode_lookup_accepts_onspd_columns():
    source = Path(__file__).parent / "fixtures" / "onspd_sample.csv"
    result = load_postcode_lookup(source)

    assert result.loc[0, "_postcode_key"] == "AA11AA"
    assert result.loc[0, "_easting"] == 530_000
