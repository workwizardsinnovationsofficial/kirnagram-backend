from app.models.post import normalize_post_ratio


def test_normalize_post_ratio_accepts_supported_values() -> None:
    assert normalize_post_ratio("9:16") == "9:16"
    assert normalize_post_ratio("16:9") == "16:9"
    assert normalize_post_ratio("1:1") == "1:1"


def test_normalize_post_ratio_falls_back_for_unknown_values() -> None:
    assert normalize_post_ratio("4:5") == "1:1"
    assert normalize_post_ratio(None) == "1:1"
