"""Synthetic fixtures test research boundaries, never investment performance."""
import copy
from datetime import date, timedelta
import json
import unittest

from app.engine import DEFAULT_WEIGHTS
from app.optimization import CHECKPOINT, FACTOR_KEYS, optimize_sessions


def row(index, label, factors=None, weights=None):
    values = {key: 50.0 for key in FACTOR_KEYS}
    if factors:
        values.update(factors)
    chosen = weights or DEFAULT_WEIGHTS
    score = round(sum(chosen[key] * values[key] for key in FACTOR_KEYS), 4)
    return {"thscode": f"{index + 1:06d}.SZ", "label": label, "score": score,
            "factors": {key: {"score": value} for key, value in values.items()}}


def sessions(days=30, count=20):
    result = []
    for day_index in range(days):
        rows = []
        for index in range(count):
            positive = index < 5
            rows.append(row(index, positive, {"gap": 100.0 if positive else 0.0,
                                             "late_momentum": 0.0 if positive else 100.0}))
        result.append({"date": (date(2026, 1, 1) + timedelta(days=day_index)).isoformat(),
                       "mode": "live", "checkpoint": CHECKPOINT, "rows": rows})
    return result


class OptimizationTests(unittest.TestCase):
    def test_carried_volume_ratio_is_counted_and_disclosed(self):
        data = sessions(2, 4)
        for item in data:
            for current in item["rows"]:
                current["factors"]["volume_ratio"] = {"score": 50.0, "value_source": "carried",
                                                     "value_age_seconds": 580,
                                                     "carried_from": "2026-01-01T09:15:10+08:00"}
        result = optimize_sessions(data, DEFAULT_WEIGHTS)
        summary = result["sample_summary"]
        self.assertEqual(summary["complete_days"], 2)
        self.assertEqual(summary["volume_ratio_carried_rows"], 8)
        self.assertEqual(summary["days"][0]["volume_ratio_carried_rows"], 4)
        self.assertTrue(any("有界携带" in warning for warning in result["warnings"]),
                        "使用携带值必须在实验警告里披露")

    def test_empty_has_no_recommendation_and_no_fake_zero_precision(self):
        result = optimize_sessions([], DEFAULT_WEIGHTS)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIsNone(result["candidate_weights"])
        self.assertFalse(result["recommendation"]["accepted"])
        self.assertIsNone(result["baseline"]["precision"])
        self.assertIsNone(result["baseline"]["wilson_interval"])

    def test_short_sample_returns_actual_selection_and_null_zero_base_lift(self):
        data = sessions(2, 4)
        for item in data:
            for current in item["rows"]:
                current["label"] = False
        result = optimize_sessions(data, DEFAULT_WEIGHTS)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["baseline"]["selected_count"], 8)
        self.assertEqual(result["baseline"]["precision"], 0)
        self.assertIsNone(result["baseline"]["recall"])
        self.assertIsNone(result["baseline"]["lift"])
        self.assertEqual(result["baseline"]["daily"][0]["selected_count"], 4)

    def test_unknown_label_not_counted_as_a_loss_and_day_excluded_from_search(self):
        data = sessions(2, 8)
        data[0]["rows"][0]["label"] = None
        result = optimize_sessions(data, DEFAULT_WEIGHTS)
        self.assertEqual(result["sample_summary"]["excluded"]["unknown_label_rows"], 1)
        self.assertEqual(result["sample_summary"]["complete_days"], 1)
        self.assertEqual(result["baseline"]["pool_count"], 15)
        self.assertEqual(result["baseline"]["positive_count"], 9)
        self.assertFalse(result["baseline"]["daily"][0]["complete"])

    def test_demo_and_reconstruction_cannot_enter_or_contaminate_live_day(self):
        data = sessions(2)
        demo = copy.deepcopy(data[0])
        demo["mode"] = "demo"
        reconstructed = copy.deepcopy(data[1])
        reconstructed["mode"] = "reconstructed"
        missing_mode = copy.deepcopy(data[0])
        del missing_mode["mode"]
        result = optimize_sessions(data + [demo, reconstructed, missing_mode], DEFAULT_WEIGHTS)
        self.assertEqual(result["sample_summary"]["complete_days"], 2)
        self.assertEqual(result["sample_summary"]["excluded"]["non_live_session"], 3)

    def test_explicit_bad_quality_excluded_even_when_mode_live(self):
        data = sessions(2)
        data[0]["quality"] = {"eligible_for_optimization": False}
        result = optimize_sessions(data, DEFAULT_WEIGHTS)
        self.assertEqual(result["sample_summary"]["complete_days"], 1)
        self.assertEqual(result["sample_summary"]["excluded"]["ineligible_quality_session"], 1)

    def test_duplicate_dates_exclude_both_records_without_order_preference(self):
        data = sessions(2)
        data.append(copy.deepcopy(data[0]))
        result = optimize_sessions(data, DEFAULT_WEIGHTS)
        reverse = optimize_sessions(list(reversed(data)), DEFAULT_WEIGHTS)
        self.assertEqual(result, reverse)
        self.assertEqual(result["sample_summary"]["excluded"]["duplicate_date_session"], 2)
        self.assertEqual(result["sample_summary"]["complete_days"], 1)

    def test_duplicate_stock_excludes_all_duplicates_and_marks_incomplete_day(self):
        data = sessions(1)
        data[0]["rows"].append(copy.deepcopy(data[0]["rows"][0]))
        result = optimize_sessions(data, DEFAULT_WEIGHTS)
        self.assertEqual(result["sample_summary"]["excluded"]["duplicate_code_rows"], 2)
        self.assertEqual(result["sample_summary"]["complete_days"], 0)
        self.assertEqual(result["baseline"]["pool_count"], 19)

    def test_bad_date_checkpoint_and_invalid_code_are_reported(self):
        data = sessions(4)
        data[0]["date"] = "2026-02-30"
        data[1]["checkpoint"] = "09:25:00"
        data[2]["rows"][0]["thscode"] = "1"
        result = optimize_sessions(data, DEFAULT_WEIGHTS)
        excluded = result["sample_summary"]["excluded"]
        self.assertEqual(excluded["invalid_date_session"], 1)
        self.assertEqual(excluded["checkpoint_mismatch_session"], 1)
        self.assertEqual(excluded["invalid_code_rows"], 1)
        self.assertEqual(result["sample_summary"]["complete_days"], 1)

    def test_bool_nonfinite_and_missing_factor_scores_are_not_valid(self):
        for bad_value in (True, False, None, float("nan"), float("inf"), -1, 101, "50"):
            with self.subTest(bad_value=bad_value):
                data = sessions(1)
                data[0]["rows"][0]["factors"]["retention"]["score"] = bad_value
                result = optimize_sessions(data, DEFAULT_WEIGHTS)
                self.assertEqual(result["sample_summary"]["missing_factors"]["retention"], 1)
                self.assertEqual(result["baseline"]["pool_count"], 19)
                self.assertEqual(result["sample_summary"]["complete_days"], 1)
                json.dumps(result, allow_nan=False)

    def test_zero_factor_and_score_are_valid(self):
        data = sessions(1, 1)
        data[0]["rows"] = [row(0, True, {key: 0 for key in FACTOR_KEYS})]
        result = optimize_sessions(data, DEFAULT_WEIGHTS)
        self.assertEqual(result["baseline"]["pool_count"], 1)
        self.assertEqual(result["baseline"]["precision"], 1)

    def test_unranked_baseline_excluded_even_if_all_factors_are_complete(self):
        data = sessions(1)
        data[0]["rows"][0]["score"] = None
        result = optimize_sessions(data, DEFAULT_WEIGHTS)
        self.assertEqual(result["sample_summary"]["excluded"]["unranked_rows"], 1)
        self.assertEqual(result["sample_summary"]["complete_days"], 1)
        self.assertEqual(result["baseline"]["pool_count"], 19)

    def test_disabled_factor_does_not_change_fixed_common_coverage(self):
        data = sessions(30)
        for session in data:
            session["rows"][0]["factors"]["retention"]["score"] = None
        weights = dict(DEFAULT_WEIGHTS)
        weights["retention"] = 0
        result = optimize_sessions(data, weights)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["sample_summary"]["common_rows"], 570)
        self.assertEqual(result["holdout"]["baseline"]["pool_count"], 114)
        self.assertEqual(result["holdout"]["candidate"]["pool_count"], 114)

    def test_minimum_days_rows_and_development_classes_are_enforced(self):
        self.assertEqual(optimize_sessions(sessions(29), DEFAULT_WEIGHTS)["status"], "insufficient_data")
        self.assertEqual(optimize_sessions(sessions(30, 9), DEFAULT_WEIGHTS)["status"], "insufficient_data")
        data = sessions(30)
        for day in data[:24]:
            for current in day["rows"]:
                current["label"] = False
        result = optimize_sessions(data, DEFAULT_WEIGHTS)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIsNone(result["candidate_weights"])
        self.assertEqual(result["sample_summary"]["development_positive_count"], 0)

    def test_walk_forward_splits_whole_dates_before_holdout(self):
        result = optimize_sessions(sessions(), DEFAULT_WEIGHTS)
        self.assertEqual(result["status"], "completed")
        self.assertLessEqual(result["search"]["candidate_count"], 64)
        self.assertEqual(len(result["search"]["folds"]), 3)
        holdout = set(result["search"]["holdout_dates"])
        self.assertEqual(len(holdout), 6)
        validation_dates = set()
        last_train_count = 0
        for fold in result["search"]["folds"]:
            train, validation = fold["training_dates"], fold["validation_dates"]
            self.assertGreaterEqual(len(train), 10)
            self.assertGreaterEqual(len(validation), 3)
            self.assertGreater(len(train), last_train_count)
            self.assertLess(max(train), min(validation))
            self.assertFalse(set(train) & holdout)
            self.assertFalse(set(validation) & holdout)
            self.assertFalse(set(validation) & validation_dates)
            self.assertLessEqual(len(fold["nominees"]), 3)
            validation_dates.update(validation)
            last_train_count = len(train)
        self.assertFalse(result["search"]["holdout_used_for_selection"])

    def test_holdout_labels_never_change_selected_weights_or_factor_diagnostics(self):
        original = sessions()
        changed = copy.deepcopy(original)
        for session in changed[-6:]:
            for current in session["rows"]:
                current["label"] = not current["label"]
        first = optimize_sessions(original, DEFAULT_WEIGHTS)
        second = optimize_sessions(changed, DEFAULT_WEIGHTS)
        self.assertEqual(first["candidate_weights"], second["candidate_weights"])
        self.assertEqual(first["search"], second["search"])
        self.assertEqual(first["factor_diagnostics"], second["factor_diagnostics"])
        self.assertNotEqual(first["holdout"]["precision_delta"], second["holdout"]["precision_delta"])
        self.assertTrue(first["recommendation"]["accepted"])
        self.assertFalse(second["recommendation"]["accepted"])

    def test_repeated_runs_and_input_order_are_deterministic_and_inputs_unchanged(self):
        data = sessions()
        frozen = copy.deepcopy(data)
        first = optimize_sessions(data, DEFAULT_WEIGHTS)
        permuted = list(reversed(copy.deepcopy(data)))
        for session in permuted:
            session["rows"].reverse()
        second = optimize_sessions(permuted, DEFAULT_WEIGHTS)
        self.assertEqual(first, second)
        self.assertEqual(data, frozen)
        self.assertFalse(first["recommendation"]["automatic_application"])
        json.dumps(first, allow_nan=False)

    def test_all_equal_scores_prefer_baseline_and_code_order(self):
        data = sessions()
        for session in data:
            session["rows"] = [row(index, index < 5) for index in range(20)]
        result = optimize_sessions(data, DEFAULT_WEIGHTS)
        self.assertEqual(result["search"]["selected_candidate"], "baseline")
        self.assertFalse(result["recommendation"]["accepted"])
        self.assertEqual(result["holdout"]["baseline"]["daily"][0]["selected_codes"][0], "000001.SZ")

    def test_candidate_acceptance_is_only_a_proposal_with_explicit_thresholds(self):
        result = optimize_sessions(sessions(), DEFAULT_WEIGHTS)
        self.assertTrue(result["recommendation"]["accepted"])
        self.assertGreaterEqual(result["holdout"]["precision_delta"], .03)
        self.assertGreaterEqual(result["holdout"]["positive_gain_day_ratio"], .6)
        self.assertGreater(result["recommendation"]["validation_precision_delta"], 0)
        self.assertEqual(result["holdout"]["evaluation_count"], 1)
        self.assertFalse(result["recommendation"]["automatic_application"])
        self.assertEqual(len(result["factor_diagnostics"]), 7)
        self.assertTrue(all(item["period"] == "development_only" for item in result["factor_diagnostics"]))

    def test_cancellation_never_leaves_an_applicable_candidate(self):
        calls = 0

        def stop():
            nonlocal calls
            calls += 1
            return calls > 90

        result = optimize_sessions(sessions(), DEFAULT_WEIGHTS, should_stop=stop)
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["candidate_weights"])
        self.assertIsNone(result["holdout"])
        self.assertFalse(result["recommendation"]["accepted"])

    def test_invalid_options_raise_controlled_errors(self):
        for top_k in (True, 0, 101, 1.5, "10"):
            with self.assertRaises(ValueError):
                optimize_sessions([], DEFAULT_WEIGHTS, top_k=top_k)
        for weights in ({"unknown": 1}, {"gap": True}, {"gap": float("inf")}, {}):
            with self.assertRaises(ValueError):
                optimize_sessions([], weights)
        with self.assertRaises(ValueError):
            optimize_sessions({}, DEFAULT_WEIGHTS)

    def test_single_active_weight_and_ablation_keep_valid_finite_candidates(self):
        data = sessions()
        weights = {key: float(key == "gap") for key in FACTOR_KEYS}
        for session in data:
            for current in session["rows"]:
                current["score"] = current["factors"]["gap"]["score"]
        result = optimize_sessions(data, weights)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["search"]["selected_candidate"], "baseline")
        self.assertFalse(result["recommendation"]["accepted"])
        self.assertIsNone(next(item for item in result["factor_diagnostics"] if item["factor"] == "gap")["without_factor"])
        self.assertAlmostEqual(sum(result["candidate_weights"].values()), 1)
        json.dumps(result, allow_nan=False)

    def test_immediate_cancellation_discards_every_day(self):
        result = optimize_sessions(sessions(), DEFAULT_WEIGHTS, should_stop=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["sample_summary"]["usable_days"], 0)
        self.assertIsNone(result["candidate_weights"])


if __name__ == "__main__":
    unittest.main()
