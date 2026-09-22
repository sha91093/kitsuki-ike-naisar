from scripts.fetch_weather import response_to_frame


def test_response_to_frame_handles_missing_value():
    frame = response_to_frame(
        {
            "daily": {
                "time": ["2026-07-05", "2026-07-06"],
                "precipitation_sum": [54.4, None],
            }
        }
    )

    assert frame.to_dict(orient="records") == [
        {"date": "2026-07-05", "precipitation_mm": 54.4},
        {"date": "2026-07-06", "precipitation_mm": 0.0},
    ]
