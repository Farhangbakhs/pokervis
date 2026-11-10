"""The read trigger and the motion gate, on synthetic frames with the real TableReader."""
import numpy as np

from vision.detect import RawDet
from vision.reader import TableReader

AREAS = {"BOARD": [0.3, 0.3, 0.6, 0.8], "S1": [0.65, 0.3, 0.95, 0.8]}
W, H = 1080, 1920


class FakeDet:
    """Sees a card wherever a crop has a bright-enough rectangle. Counts its reads."""

    def __init__(self, floor):
        self.floor, self.calls = floor, 0

    def detect(self, crop):
        self.calls += 1
        m = crop.mean(2) > self.floor
        if m.sum() < 3000:
            return []
        ys, xs = np.where(m)
        b = (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))
        return [RawDet(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), b, "As", 0.8, "As")]


def frame(rng, card=0, hand_over=None, exposure=0):
    f = 170 + exposure + rng.integers(-1, 2, (H, W, 3))
    x1, y1, x2, y2 = (int(AREAS["BOARD"][0] * W), int(AREAS["BOARD"][1] * H), int(AREAS["BOARD"][2] * W), int(AREAS["BOARD"][3] * H))
    if card:
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        f[cy - 110:cy + 110, cx - 80:cx + 80] = 170 + exposure + card
    if hand_over == "S1":
        f[int(.35 * H):int(.75 * H), int(.7 * W):int(.9 * W)] = rng.integers(40, 90)
    return np.clip(f, 0, 255).astype(np.uint8)


def run(reader, frames):
    t, stable_at = 0.0, None
    for i, f in enumerate(frames):
        t += 0.25
        reader.process(f, t, AREAS, [], t)
        if stable_at is None and any(z and z["stable"] for z in reader.trk.message()["zones"].values()):
            stable_at = i
    return stable_at


def test_a_low_contrast_card_is_read_after_the_table_has_been_idle():
    """Regression: an area-wide average missed cards like this forever, because the read budget was already spent."""
    for contrast in (18, 22, 30, 60):
        rng, det = np.random.default_rng(1), FakeDet(170 + contrast / 2)
        reader = TableReader(det)
        idle = [frame(rng) for _ in range(12)]
        assert run(reader, idle) is None
        spent = det.calls
        at = run(reader, [frame(rng, card=contrast) for _ in range(5)])
        assert det.calls > spent, f"contrast {contrast}: card never triggered a read"
        assert at in (2, 3), f"contrast {contrast}: three still frames should confirm it, got {at}"


def test_an_idle_table_costs_no_model_reads():
    rng, det = np.random.default_rng(2), FakeDet(200)
    reader = TableReader(det)
    run(reader, [frame(rng) for _ in range(40)])
    assert det.calls == 3                                  # three looks at an empty area, then nothing


def test_phone_auto_exposure_does_not_trigger_reads():
    rng, det = np.random.default_rng(3), FakeDet(250)
    reader = TableReader(det)
    run(reader, [frame(rng) for _ in range(10)])
    spent = det.calls
    run(reader, [frame(rng, exposure=12) for _ in range(10)])
    assert det.calls == spent


def test_a_hand_over_a_seat_does_not_hold_back_the_board():
    rng, det = np.random.default_rng(4), FakeDet(200)
    reader = TableReader(det)
    hands = [frame(rng, card=60, hand_over="S1" if i % 2 else None) for i in range(5)]
    assert run(reader, hands) == 2                         # S1 is not even watched, and the board goes on regardless


def test_seats_are_only_read_when_the_server_says_so_and_forgotten_after():
    rng, det = np.random.default_rng(5), FakeDet(200)
    reader = TableReader(det)
    f = frame(rng)
    reader.process(f, 1.0, AREAS, [], 1.0)
    assert "S1" not in reader.sig
    reader.process(frame(rng), 2.0, AREAS, [1], 2.0)
    assert "S1" in reader.sig
    reader.process(frame(rng), 3.0, AREAS, [], 3.0)
    assert "S1" not in reader.sig


def test_one_model_read_per_frame_and_areas_take_turns():
    rng, det = np.random.default_rng(6), FakeDet(250)
    reader = TableReader(det)
    order = []
    for i in range(6):
        t = float(i + 1)
        read, _ = reader.process(frame(rng), t, AREAS, [1], t)
        assert len(read) <= 1
        order += read
    assert order == ["BOARD", "S1", "BOARD", "S1", "BOARD", "S1"]          # three votes each, neither area starves


def test_black_frames_warn_and_read_nothing():
    det = FakeDet(200)
    reader = TableReader(det)
    read, warnings = reader.process(np.zeros((H, W, 3), np.uint8), 1.0, AREAS, [], 1.0)
    assert read == [] and det.calls == 0 and "BLACK" in warnings[0]
