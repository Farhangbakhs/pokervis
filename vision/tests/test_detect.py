import numpy as np

from vision.detect import RawDet, one_read_per_card, same_card


def det(code: str, conf: float, x: float, y: float) -> RawDet:
    return RawDet((x, y), (x - 15, y - 10, x + 15, y + 10), code, conf, code)


def table_with_two_cards() -> np.ndarray:
    img = np.full((400, 400, 3), 165, np.uint8)            # grey table
    img[20:160, 40:240] = 225                              # card 1
    img[200:340, 40:240] = 225                             # card 2 (a strip of table between them)
    img[80:100, 130:150] = (30, 30, 200)                   # a red pip on card 1
    return img


def test_same_card_stays_on_the_card_surface_and_ignores_pips():
    img = table_with_two_cards()
    assert same_card(img, (60, 40), (220, 140))            # opposite corners of card 1, line crosses the pip
    assert not same_card(img, (220, 140), (60, 220))       # corner of card 1 to corner of card 2


def test_misread_corner_is_dropped_keeping_the_stronger_read():
    img = table_with_two_cards()
    out = one_read_per_card(img, [det("Ah", .76, 220, 40), det("4h", .49, 60, 140)])
    assert [d.code for d in out] == ["Ah"]


def test_a_card_read_on_both_corners_is_kept_and_real_neighbours_survive():
    img = table_with_two_cards()
    dets = [det("9h", .7, 220, 40), det("9h", .7, 60, 140), det("Ks", .5, 220, 220)]
    assert [d.code for d in one_read_per_card(img, dets)] == ["9h", "9h", "Ks"]
