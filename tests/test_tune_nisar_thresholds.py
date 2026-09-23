import numpy as np

from scripts.tune_nisar_thresholds import (
    classification_metrics,
    region_statistics,
    select_joint_thresholds,
    threshold_values,
    top_threshold_candidates,
)


def test_classification_metrics_counts_pixel_errors():
    prediction = np.array([[True, True], [False, False]])
    truth = np.array([[True, False], [True, False]])
    metric = classification_metrics(prediction, truth, np.ones((2, 2), dtype=bool))
    assert metric["iou"] == 1 / 3
    assert metric["precision"] == 0.5
    assert metric["recall"] == 0.5
    assert (metric["tp_pixels"], metric["fp_pixels"], metric["fn_pixels"]) == (1, 1, 1)


def test_threshold_values_includes_stop():
    assert threshold_values(-2, -1, 0.25).tolist() == [-2.0, -1.75, -1.5, -1.25, -1.0]


def test_joint_threshold_search_prefers_matching_consensus():
    truth = np.array([[True, False], [False, False]])
    orbit = {
        "hh": np.array([[-12.0, -5.0], [-4.0, -3.0]]),
        "hv": np.array([[-18.0, -10.0], [-9.0, -8.0]]),
        "valid": np.ones((2, 2), dtype=bool),
    }
    sample = {
        "truth": truth,
        "analysis": np.ones((2, 2), dtype=bool),
        "core": truth.copy(),
        "descending": orbit,
        "ascending": orbit,
    }
    candidates = top_threshold_candidates(
        [sample], "descending", np.array([-13.0, -10.0]), np.array([-19.0, -15.0]), 2
    )
    descending, ascending, best = select_joint_thresholds([sample], candidates, candidates, 1)
    assert descending["hh_threshold_db"] == -10.0
    assert ascending["hv_threshold_db"] == -15.0
    assert best["mean_iou"] == 1.0


def test_region_statistics_ignores_nan_and_outside_values():
    values = np.array([[1.0, 2.0], [np.nan, 10.0]])
    region = np.array([[True, True], [True, False]])
    stats = region_statistics(values, region)
    assert stats == {"pixels": 2, "p25_db": 1.25, "median_db": 1.5, "p75_db": 1.75}
