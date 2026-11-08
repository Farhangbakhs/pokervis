# Run AutoPoker

Everyone (players on phones, the big screen) uses ONE page: `/` (the pixel-art UI in `web/`, built on the teammate's
`pixel.js` + `style.css`). The dealer has a separate console at `/dealer`. Chips are CHIP play money on Solana **devnet**.

## Start
```
scripts\dev.ps1                       # chain service + game server (real devnet).  -Mock = fake ledger
.venv\Scripts\python.exe -m vision.service --source web:http://127.0.0.1:8000     # the card reader
.venv\Scripts\python.exe tools\cam_link.py                                        # https tunnel + QR for the camera phone
```
Dealer PIN is `DEALER_PIN` in `.env`.

## Camera
1. Camera phone: open the `/cam` link (QR from `cam_link.py`), enter the PIN, Start camera. Mount ~1 m above, pointing down.
2. Dealer phone/laptop: `/dealer` -> **edit table areas**. Draw the **Board** area and each **Seat** area by dragging.
   Make them big. Cards can sit anywhere inside their area.

## Players
Scan the seat QR from the dealer page (Seat 1..4), type a name, **Sit down**. A free guest wallet is made on the phone.
No wallet app and no SOL needed. Chips are CHIP play money; winnings are paid on-chain to that wallet.
No second human? `tools\bot_player.py --seat 2` plays as a calling station.

## How it plays
- The camera is the truth. Every screen shows exactly the cards it sees in the Board area, immediately, always.
- **Nothing starts by itself.** Between rounds the table waits: players sit down and buy in / top up on their phones.
  When everyone is in, the dealer presses **START ROUND?** on `/dealer`. It is refused (with the reason shown on every screen) if
  fewer than 2 players have chips, or if the camera still sees cards on the board ("CLEAR THE BOARD").
- Players get 2 cards face down and bet on their phones.
- When **5 cards are out** on the board, the reader looks at each remaining player's area for their two cards, the best hand
  wins, and the winner is paid on Solana. Nobody's private look at their own cards is ever scanned.
- The result stays on screen for a few seconds, then the table goes back to waiting for the dealer.
- Cards left over from the last round are ignored until they are picked up.
- The dealer drawer shows the camera view, the areas editor and an emergency reset.

## Tests
`python -m pytest game vision server` · `cd chain\service && npm test` · `python tools\e2e_full.py` (services running)
Experiment scripts (model bake-off, gallery) live in `tools/experiments/`.
To pick up teammate UI updates: copy their `pixel.js` and `style.css` into `web/vendor/`.
