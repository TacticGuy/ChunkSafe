"""Phase 3.3: the demo script runs end to end and all its checks hold."""
import demo


def test_demo_runs(tmp_path):
    assert demo.main(["--out", str(tmp_path)]) == 0
