"""Ground-truth card per video id (read by eye from contact sheets). Unknown/ambiguous videos omitted."""

GT = {
    # clubs
    "6106": "Jc", "6107": "8c", "6109": "Kc", "6112": "Tc", "6115": "2c", "6118": "Kc", "6127": "Qc",
    "6129": "8c", "6132": "7c", "6143": "Ac", "6146": "Ac", "6148": "Tc", "6158": "5c",
    # diamonds
    "6113": "Td", "6117": "9d", "6121": "Td", "6122": "7d", "6125": "8d", "6133": "Qd", "6135": "Ad",
    "6136": "7d", "6137": "4d", "6138": "6d", "6141": "Ad", "6145": "5d", "6147": "Jd", "6159": "Kd",
    "6155": "Qd",  # from the Queens folder (mid-frame in Diamonds was empty)
    # hearts
    "6108": "Qh", "6110": "7h", "6123": "2h", "6124": "5h", "6126": "6h", "6130": "3h", "6131": "9h",
    "6134": "2h", "6139": "7h", "6140": "8h", "6142": "4h", "6150": "5h", "6151": "3h", "6154": "8h",
    # spades
    "6111": "Ts", "6114": "8s", "6116": "7s", "6119": "As", "6153": "8s", "6157": "Ks",
    "6162": "Qs",  # new, Queens folder
}


def norm(name: str) -> str:
    """'10H', '10 of hearts', 'Ah' -> pokerkit code like 'Th', 'Ah'."""
    n = name.strip().lower().replace(" of ", "").replace("_", "")
    suits = {"hearts": "h", "heart": "h", "spades": "s", "spade": "s", "clubs": "c", "club": "c",
             "diamonds": "d", "diamond": "d"}
    for k, v in suits.items():
        if n.endswith(k):
            n = n[:-len(k)] + v
            break
    r, s = n[:-1].upper(), n[-1]
    r = {"10": "T", "1": "A", "ACE": "A", "KING": "K", "QUEEN": "Q", "JACK": "J"}.get(r, r)
    return r + s
