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

## Wallet, chips and history (Solana devnet + Postgres)
- **Login = wallet.** Scanning a seat QR signs a one-time challenge with the phone's Solana wallet (made automatically, no app, no SOL).
  The wallet key lives in the browser: `MY WALLET & STATS` -> *Copy wallet key* to play from another phone, *Restore* to load it.
- **Chips are CHIP (devnet play money).** First visit (or busted) = free 1000 CHIP grant. After that the on-chain bankroll IS your stack:
  win 300, **LOG OUT**, scan again later -> you sit down with 1300. Refill is only offered at 0 chips.
- **Payout is instant on screen**, the Solana transaction confirms in the background (about 1 s) and the Explorer link appears under the status bar.
- **History:** every grant and hand is written to Postgres (Tiger Cloud, `DATABASE_URL` in `.env`; tables are created automatically).
  `MY WALLET & STATS` shows the chip balance after every hand (x = hands, y = CHIP), win rate, net, and links to each payout transaction.
  `GET /api/history?pubkey=` returns the same data. The game never waits on the DB: if it is down, play continues.

## Bot opponent
`scripts\dev.ps1 -Bot` (or `python tools\bot_player.py --seat 2 --style normal|loose|station`) seats a bot that signs in with its own
Solana wallet like a phone, checks/calls/raises/folds at random, refills when busted and re-sits if kicked. Its cards are physical:
at showdown put its two cards face up in its seat area (or use the dealer override).

## Chip price
`LAMPORTS_PER_CHIP` in `.env` sets the price: **1000 lamports per CHIP, so 1000 CHIP = 0.001 SOL**. Change it only together with a fresh CHIP mint
(move `chain/service/.state` aside, `npm run setup`, delete `data/table_state.json`) because chips already minted were priced at the old rate.
If Phantom says not enough SOL: the join screen shows the connected address and its REAL devnet balance; Phantom must also be on
Settings > Developer Settings > Testnet Mode > Solana Devnet.

## One-command start (no bots)
`run.cmd` (double-click) or `powershell -ExecutionPolicy Bypass -File scripts\run.ps1` stops any old run, starts the chain service, game server,
HTTPS tunnel and card reader, retries the tunnel if Cloudflare is slow, makes a fresh camera QR (`data\cam_qr.png`) and prints/saves every link
(`data\links.txt`). `-KeepTable` keeps last run seat codes, `-NoVision` skips the card reader. `scripts\stop.ps1` stops everything.
