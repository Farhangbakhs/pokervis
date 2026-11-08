import numpy as np
import pytest

from vision import areas


def test_validate_accepts_defaults_and_rejects_bad():
    assert set(areas.validate(areas.DEFAULT_AREAS)) == set(areas.NAMES)
    for bad in ({"NOPE": [0, 0, .5, .5]}, {"S1": [0.5, 0.5, 0.4, 0.9]}, {"S1": [0, 0, 1.2, .5]},
                {"S1": [0, 0, .5]}, {"S1": [0.1, 0.1, 0.11, 0.9]}):
        with pytest.raises(ValueError):
            areas.validate(bad)


def test_crop_and_labels_and_save_roundtrip(tmp_path):
    f = np.zeros((200, 100, 3), np.uint8)
    c, (x0, y0) = areas.crop(f, [0.5, 0.25, 1.0, 0.75])
    assert c.shape[:2] == (100, 50) and (x0, y0) == (50, 50)
    assert [areas.label_for("BOARD", i) for i in range(3)] == ["B1", "B2", "B3"]
    assert [areas.label_for("S2", i) for i in range(2)] == ["2a", "2b"]
    p = tmp_path / "a.json"
    areas.save({"BOARD": [0.1, 0.1, 0.5, 0.5]}, p)
    assert areas.load(p) == {"BOARD": [0.1, 0.1, 0.5, 0.5]}
    p.write_text("{corrupt")
    assert areas.load(p) == areas.DEFAULT_AREAS
