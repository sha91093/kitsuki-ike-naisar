import pandas as pd

from scripts.build_dashboard_data import antecedent_rain, rainfall_lookup


def test_antecedent_rain_includes_observation_day():
    weather = pd.DataFrame(
        {
            "date": ["2026-07-04", "2026-07-05", "2026-07-06"],
            "precipitation_mm": [30.2, 54.4, 24.2],
        }
    )
    lookup = rainfall_lookup(weather)

    assert antecedent_rain(lookup, pd.Timestamp("2026-07-06"), 1) == 24.2
    assert antecedent_rain(lookup, pd.Timestamp("2026-07-06"), 3) == 108.8
