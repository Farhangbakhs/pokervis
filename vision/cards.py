"""Card-name helpers shared by the vision code."""
from __future__ import annotations

_SUITS = {"hearts": "h", "heart": "h", "spades": "s", "spade": "s", "clubs": "c", "club": "c",
          "diamonds": "d", "diamond": "d"}
_RANKS = {"10": "T", "1": "A", "ACE": "A", "KING": "K", "QUEEN": "Q", "JACK": "J"}


def norm_label(name: str) -> str:
    """Model class name ('10H', '10 of hearts', 'Ah') -> pokerkit code ('Th', 'Ah')."""
    n = name.strip().lower().replace(" of ", "").replace("_", "")
    for k, v in _SUITS.items():
        if n.endswith(k):
            n = n[:-len(k)] + v
            break
    r, s = n[:-1].upper(), n[-1]
    return _RANKS.get(r, r) + s
