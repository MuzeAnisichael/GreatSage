import pytest
from greatsage.evaluation import classification, compare_reports, distribution, recognition


def test_recognition_does_not_hide_number_or_homophone_errors():
    assert recognition("你好，世界！", "你好世界")["cer"] == 0
    assert recognition("日期12日", "日期13日")["cer"] > 0
    assert recognition("几", "机")["cer"] == 1
    assert recognition("hello world", "hello new world")["wer"] == .5


def test_report_metrics_and_incompatible_corpus():
    assert distribution([1, 2, 3, 4, 5])["p95"] == 4.8
    assert distribution([])["count"] == 0
    scores = classification([{"expected": True, "actual": False}, {"expected": False, "actual": True}])
    assert scores["false_positive"] == scores["false_negative"] == 1
    with pytest.raises(ValueError):
        compare_reports({"suite": "voice", "corpus_sha256": "a"}, {"suite": "voice", "corpus_sha256": "b"})
