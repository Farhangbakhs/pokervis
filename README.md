# PokerVis

A camera deals the poker. A phone above a real table reads the physical cards with computer vision, and everyone's screen follows the game: betting on phones, the board and the pot on the big screen, and the winner worked out from the cards on the felt.

## How it works

```
phone camera ──frames──▶ game server ──▶ card reader (YOLOv8 + voting)
                             ▲                     │
   players' phones ◀──state──┤◀──────cards─────────┘
   big screen      ◀──state──┘
```

- **Reading cards.** Each play area is cropped and read only when its picture changes, so an idle table costs nothing. A card must be read in 3 of the last 4 looks before it counts. The moment the camera spots one, screens show a face-down "scanning" card. It flips once the votes are in.
- **Privacy.** Seat areas are only read once all five board cards are out, so a player peeking at their own cards is never recorded.
- **Chips.** CHIP is a play-money token on Solana devnet, bought with devnet SOL and paid out on chain after every hand.
- **Tables.** Rules and hand ranking come from [pokerkit](https://github.com/uoftcsail/pokerkit). The model is a 52-class YOLOv8l chosen by a bake-off on our own footage, plus a small nearest-neighbour check for one card family it misreads.

## Two ways to run it

**Hosted tables** (`HOSTED=1`): anyone can open a short-lived table and play with a Solana devnet wallet. Bots are optional. See `Dockerfile` and `fly.toml`.

**Your own table** on your own machine: see [RUN.md](RUN.md).

## Tests

```
python -m pytest game vision server
cd chain/service && npm test
```
