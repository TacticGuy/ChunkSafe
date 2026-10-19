"""Evaluation sanity checks: the safety pass never misses an unsafe single statement."""
import evaluate


def test_safety_recall_is_perfect_on_single_statements(tmp_path):
    res = evaluate.eval_safety(tmp_path)
    assert res["single_statement"]["recall"] == 1.0
    assert res["all_errors_or_warnings"]["recall"] == 1.0


def test_crash_correctness_quick(tmp_path):
    res = evaluate.eval_correctness(tmp_path, random_trials=3)
    assert res["every_boundary"]["match_rate"] == 1.0
    assert res["random_schedules"]["match_rate"] == 1.0
