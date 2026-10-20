from app import config


def test_threshold_in_range():
    assert 0.0 < config.MATCH_THRESHOLD < 1.0


def test_cameras_have_direction():
    for cam in config.CAMERAS:
        assert cam["direction"] in ("IN", "OUT")
