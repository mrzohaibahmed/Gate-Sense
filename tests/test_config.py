from app import config


def test_threshold_in_range():
    assert 0.0 < config.MATCH_THRESHOLD < 1.0


def test_cameras_have_name_source_direction():
    names = [cam["name"] for cam in config.CAMERAS]
    assert len(names) == len(set(names))
    for cam in config.CAMERAS:
        assert cam["direction"] in ("IN", "OUT")
        assert "source" in cam


def test_vote_fits_in_kept_frames():
    assert 1 <= config.VOTE_MIN_AGREE <= config.BEST_FRAMES_PER_TRACK
