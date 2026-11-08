# Project: Camera-Dealt Poker Table with Solana Settlement

Hackathon project (Solana track). A live, in-person Texas Hold'em table:
- An overhead phone camera reads cards as the dealer places them on the table.
- Players bet from their phones (Fold / Check / Call / Raise).
- At showdown the camera reads the players' face-up cards, the engine finds the winner(s),
  and the pot is settled on Solana **devnet** using a play-money SPL token called **CHIP**.
- A big-screen web view shows the table, seats, balances, pot, board, winner and the
  Solana Explorer link for each hand's settlement transaction.

**No real money anywhere.** Devnet only. CHIP has no value. Never add mainnet config.

---

## 0. Non-negotiable principles

1. **The demo must never depend on the camera.** Every card read can be overridden by the
   dealer console (manual entry / tap to pick). Build manual entry before vision integration.
2. **Accuracy comes from controlling the problem, not a bigger model.** Fixed overhead camera,
   printed table mat with ArUco markers, fixed card zones, no overlapping cards, every card
   rectified (perspective-warped) to a standard size before classification, training data
   recorded **with the same camera, mount, and lighting** as the demo.
3. **Closed set + constraints.** One 52-card deck. Every card is unique per hand. Use this to
   resolve conflicts (linear assignment), plus temporal voting across frames, plus a human
   confirm step for low confidence.
4. **Zero-sum settlement enforced on-chain.** `settle_hand` must reject deltas that don't sum to 0.
5. Small, tested modules with clear contracts (section 3). Commit after each working step.

---

## 1. Physical setup and dealer procedure

### Table mat (top-down camera view)

```
 [ArUco 0]                                                    [ArUco 1]
                 +---------------- SEAT 3 ----------------+
                 |        [ slot 3a ]   [ slot 3b ]         |
                 +-----------------------------------------+
 +--- SEAT 4 ---+                                           +--- SEAT 2 ---+
 | [4a]  [4b]   |   [B1]  [B2]  [B3]  [B4]  [B5]            |  [2a]  [2b]  |
 +--------------+          (board / community)              +--------------+
                 +---------------- SEAT 1 ----------------+
   [ MUCK ]      |        [ slot 1a ]   [ slot 1b ]         |      [ DECK ]
                 +-----------------------------------------+
 [ArUco 3]                                                    [ArUco 2]
```
- 4 ArUco markers (DICT_4X4_50, IDs 0–3) at the corners, about 8 cm, printed on A4 and taped flat.
- Zones are drawn in "table-plane" coordinates (after homography) and saved in `vision/zones.json`.
- Each card slot is about 1.3× the size of a card. Leave at least 2 cm between slots.
- MUCK and DECK zones are ignored by vision (face-down / discard areas).
- Supports 2–4 seats.

### Dealer procedure per hand (also shown step-by-step on the dealer console)
1. Table clear. Console shows **READY** (vision sees no face-up cards in board or seat slots).
2. Dealer taps **Start hand**. Blinds are posted automatically.
3. Deal 2 cards **face down** into each active seat's slots (players may peek and put them back face down).
4. Preflop betting on phones. **Fold** = tap Fold, then slide your cards face down into MUCK.
5. Burn card face down to MUCK. **Flop:** 3 cards face up into B1, B2, B3. They must not overlap or touch slot edges. Hand out of frame.
   The console beeps when it has read them, and the board appears on the big screen.
6. Betting. Burn → **Turn** into B4. Betting. Burn → **River** into B5. Betting.
7. **Showdown:** each remaining player turns both cards face up **inside their own two slots**.
8. Big screen shows the winner and hand name. Dealer taps **Confirm** (or fixes a misread).
   Settlement transaction is sent and the Explorer link is shown.
9. Dealer collects all cards → table clear → READY.

If everyone else folds, the last player wins with no showdown and no card reads.

---

## 2. Tech stack

| Part | Choice |
|---|---|
| Vision + game server | Python 3.11+, OpenCV (with `cv2.aruco`), PyTorch + torchvision, numpy, scipy, FastAPI + uvicorn (WebSockets) |
| Poker rules | `pokerkit` (NoLimitTexasHoldem). Verify its API against current docs before use; write tests |
| On-chain program | Anchor (Rust), Solana devnet, SPL Token (CHIP, decimals 0) |
| Chain service | TypeScript (Node), `@coral-xyz/anchor` TS client + whatever web3 lib it requires, Fastify or Express |
| Web | React + Vite + TypeScript, Tailwind, framer-motion, `@solana/wallet-adapter-react` (Phantom) + guest burner-wallet fallback |
| Camera input | Phone as webcam (DroidCam / Continuity Camera / IP Webcam app) → OpenCV `VideoCapture(index or URL)` |

Repo layout:
```
CLAUDE.md
vision/        camera.py calibrate.py detect.py classify.py tracker.py service.py zones.json
training/      extract_crops.py train.py eval.py eval_multi.py build_gallery.py
game/          engine.py (pokerkit wrapper + hand state machine) tests/
server/        app.py (FastAPI: websockets + REST, wires vision + game + chain client)
chain/         programs/poker_table/ (Anchor)  tests/  service/ (TS settle service)  scripts/
web/           React app: /table  /play/:seat  /dealer  /calibrate
tools/         make_mat.py (printable ArUco markers + zone guide PDF)
data/          videos/{train,val}/  crops/{train,val}/  test_multi/   (gitignored)
models/        (gitignored)
scripts/       dev.sh (starts everything)
```

---

## 3. Contracts between modules (do not change without updating all sides)

Card code = pokerkit format: rank `23456789TJQKA` + suit `shdc`, e.g. `As`, `Td`, `7c`.
Classifier classes = 52 card codes + `BACK` + `NONE` (54 classes).

**Vision → server** (in-process call or WebSocket `/ws/vision`), emitted ~5–10 Hz:
```json
{ "ts": 1731030000.12,
  "zones": {
    "B1": {"code": "As", "conf": 0.997, "stable": true, "top3": [["As",0.997],["Ah",0.002],["Ks",0.0004]]},
    "B2": null,
    "1a": {"code": "BACK", "conf": 0.99, "stable": true, "top3": [...]}
  },
  "warnings": ["OVERLAP in B3"] }
```
`stable` = same code in ≥6 of the last 8 readings, mean conf ≥ 0.90, and the zone has been motion-free for ≥300 ms.

**Server → web** WebSocket `/ws/table`: full `TableState` (seats, names, wallet pubkeys, balances,
stack/bet per seat, folded flags, pot(s), board cards, phase, to_act, last_action, winner info,
last settlement tx signature + explorer URL, dealer prompts).

**Web → server** REST:
- `POST /api/join {seat, name, pubkey}`
- `POST /api/action {seat, type: fold|check|call|raise, amount?}`
- `POST /api/dealer/{start_hand|confirm_board|override|confirm_showdown|abort_hand|next_hand}`
- `override` body: `{zone, code}`

**Server → chain service** REST (chain service holds the server keypair):
- `POST /faucet {pubkey}` → mints 1000 CHIP to the player (+ small devnet SOL if needed)
- `POST /tx/join {pubkey, seat, amount}` → returns base64 tx (server = fee payer, partially signed) for the player's wallet to sign
- `POST /tx/cashout {pubkey, amount}` → same pattern
- `POST /submit {signedTx}` → signature
- `POST /begin_hand {hand_id}` → signature
- `POST /settle {hand_id, deltas: {pubkey: int}, showdown_hash: hex32}` → `{signature, explorer_url}`
- `POST /abort_hand {hand_id}`
- `GET /balances` → on-chain seat balances
- Env flag `SETTLEMENT_MODE=escrow|custodial`. In `custodial` mode the server wallet holds the CHIP and does plain SPL transfers. This is the fallback if the program isn't ready.

---

## 4. Vision pipeline (the accuracy-critical part)

### 4.1 Camera
- `camera.py`: open by index or URL, set the highest resolution available (prefer 4K, minimum 1080p),
  threaded reader that always returns the latest frame.
- README note: **lock focus, exposure and white balance** in the phone app. Use diffuse light and avoid glare.
  Mount the phone about 1 m above the mat. A card should be ≥120 px on its long side in the raw frame.

### 4.2 Calibration (`calibrate.py` + `/calibrate` page)
1. Detect ArUco IDs 0–3 → homography to a table-plane image at a fixed scale (2 px/mm by default).
   Re-detect each second. If the markers moved by more than 3 px, update H. If they're hidden, keep the last H.
2. Capture an **empty-table reference** in the table plane (median of 30 frames).
3. Zone editor: draw/drag rectangles for B1–B5, Na/Nb per seat, MUCK, DECK → `zones.json`.
   Default zones from the printed mat layout.

### 4.3 Card detection (`detect.py`) runs on the rectified table-plane image
1. Foreground mask = LAB absdiff vs empty reference → threshold (Otsu + floor) → morph open/close.
   Also support a brightness-threshold mode for recording without a reference.
2. `findContours` (external). For each contour use `minAreaRect`. Keep it if:
   area within ±35% of the expected card area (63×88 mm at current scale), aspect 0.62–0.80, solidity > 0.90.
   Hands/chips fail these filters.
3. Corners via `approxPolyDP` (fall back to `boxPoints`). Order corners and warp to **224×320 portrait**
   (long side vertical).
4. Contour about 1.6–2.4× card area with a card-like shape → emit `OVERLAP` warning for that zone (don't guess).
5. Assign each card to a zone by centroid point-in-polygon. Ignore MUCK/DECK.
6. Per-zone motion gate: mean absdiff vs the previous frame in that zone below threshold for ≥300 ms.
7. Debug viewer (`python -m vision.detect --debug`): rectified image with zones, contours, and a strip of warped crops.

### 4.4 Classification (`classify.py`)
- Model: torchvision `resnet18` or `mobilenet_v3_large`, ImageNet-pretrained, fine-tuned, input 320×224, 54 classes.
- Inference: **test-time augmentation**. Average softmax of the crop and the crop rotated 180°.
  Standard cards look the same rotated, so the label doesn't change. Classify the **whole rectified card**,
  not just the corner: the pip layout tells 6 from 9, and suits are clearer.
- Device: CUDA > MPS > CPU. Batch all crops in a frame.
- Fallback/baseline: `build_gallery.py` stores embeddings (pretrained backbone, penultimate layer)
  of training crops. kNN (cosine, k=5) classifier selectable by flag. Useful before the first training run finishes.

### 4.5 Tracking and constraints (`tracker.py`)
- Per zone: ring buffer of the last 8 readings → `stable` per section 3.
- **Uniqueness:** across all zones in a frame, plus cards already confirmed this hand, solve
  `scipy.optimize.linear_sum_assignment` on −log(prob) so no card code is used twice.
- Low confidence (stable but conf < 0.90, or conflicts) → dealer console shows the crop + top-3 to tap,
  plus a full 52-card picker.
- Save every low-confidence or overridden crop to `data/hard_examples/<true_code>/` for retraining.

### 4.6 Training data from videos (`training/extract_crops.py`)
Input: `data/videos/train/<CODE>.mp4` and `data/videos/val/<CODE>.mp4` (52 codes + `BACK` + `NONE`).
Each video shows exactly one card on the mat under the **real overhead camera**, moved through
every zone, rotated, with lighting varied and fingers occasionally touching edges.
`NONE.mp4` = empty mat, hands, chips, phones (negatives).
- Sample frames (stride so there are ~300 candidates per video). Rectify if markers are visible, otherwise run detection on the raw frame with scale-free filters.
- Keep frames with **exactly one** detected card. Warp it and save the PNG to `data/crops/<split>/<CODE>/`.
- Drop near-duplicates (dHash Hamming distance < 5).
- For `NONE`, save the crops of contours that pass loose filters (hard negatives).
- Print counts per class and warn if any class has < 150 crops.
- Write a **contact sheet** per class (`data/crops/_sheets/<CODE>.jpg`, 8×6 grid) so a human can spot a mislabeled video in seconds.
- Train/val split is **by video**, never by random frame (random frames leak and inflate accuracy).

### 4.7 Training (`training/train.py`) and evaluation
- Augmentations: ColorJitter(0.3, 0.3, 0.2, 0.02), small rotation ±4°, RandomPerspective(0.05),
  RandomResizedCrop(scale 0.88–1.0, keep aspect), Gaussian blur (p=0.2), JPEG quality 40–95,
  random 180° rotation (p=0.5), small random occlusion at the card edge (fingers).
- AdamW. Stage 1: head only, lr 1e-3, 2 epochs. Stage 2: full network, lr 1e-4, cosine schedule, 8–12 epochs.
  Label smoothing 0.05. Class-balanced sampler. Save the best checkpoint by val accuracy to `models/card_cls.pt`
  with the class list.
- `eval.py`: per-class accuracy, confusion matrix PNG, list of the 20 worst val crops with predictions.
- `eval_multi.py`: runs the full pipeline (detect → classify → constraints) on `data/test_multi/*.jpg`
  (real frames with 5–10 cards placed in zones, labels in `labels.json`). Reports exact-match rate per image.
- **Targets:** ≥ 99.5% per-crop val accuracy; 100% of test_multi images correct after voting + uniqueness.
  If missed: check the contact sheets, record more video for the confused classes, add hard_examples, retrain.

---

## 5. Game engine (`game/engine.py`)
- Wrap pokerkit `NoLimitTexasHoldem` for betting, blinds, min-raise rules, all-ins, side pots.
- Hole cards are physically dealt and unknown until showdown: deal them as unknown cards (`??`) if pokerkit
  supports it. At showdown, evaluate with pokerkit hand classes (`StandardHighHand`) on the revealed hole cards
  + board. Distribute each main/side pot among eligible players. Split ties evenly; the odd chip goes to the first seat left of the button.
  **Verify the pokerkit API in its docs; don't guess.** Write tests for: everyone folds, split pot,
  3-way all-in with side pots, showdown with a board-only best hand.
- Hand state machine: `WAITING → PREFLOP → FLOP_READ → FLOP → TURN_READ → TURN → RIVER_READ → RIVER → SHOWDOWN_READ → CONFIRM → SETTLING → DONE`.
  `*_READ` states wait for stable vision readings of the expected zones (or dealer override), then require `confirm_board`
  (auto-confirm after 1.5 s if all confs ≥ 0.98).
- Output of a hand: `deltas {pubkey: int}` (sum = 0), plus `showdown_hash` = sha256 of
  (hand_id, board codes, revealed hole codes per seat, JPEG bytes of the showdown frame).
- Blinds/stakes configurable (default 5/10 CHIP, buy-in 1000 CHIP).

## 6. On-chain program (`chain/programs/poker_table`, Anchor)
Accounts:
- `Table` PDA `["table", authority, table_id_le]`: authority, mint, vault (ATA owned by the Table PDA),
  hand_in_progress, current_hand_id, bump.
- `Seat` PDA `["seat", table, player]`: table, player, balance (u64), bump.

Instructions:
- `create_table(table_id)`: authority.
- `join_table(amount)`: player signs. Transfers CHIP into the vault, `init_if_needed` the Seat, adds to its balance. Fails if a hand is in progress.
- `begin_hand(hand_id)`: authority. Requires no hand in progress and `hand_id == current_hand_id + 1`.
- `settle_hand(hand_id, deltas: Vec<i64>, showdown_hash: [u8;32])`: authority. Seat accounts go in
  `remaining_accounts` in the same order as deltas. Validate each seat's owner/program/table.
  Require **sum(deltas) == 0**, no balance goes negative, and hand_id matches. Apply, clear the in-progress flag, emit a `HandSettled` event.
- `abort_hand(hand_id)`: authority, misdeal; no balance changes.
- `cash_out(amount)`: player signs. Fails if a hand is in progress. Transfers from the vault signed by the PDA.
Tests (anchor test on local validator): happy path, non-zero-sum rejected, negative balance rejected,
cash-out during a hand rejected, non-authority settle rejected, replayed hand_id rejected.

Chain scripts: `setup-devnet.ts` creates the server keypair (if missing), checks devnet SOL,
creates the CHIP mint (decimals 0), creates the table, and prints the addresses to `chain/devnet.json`.
**Fees:** the chain service is the fee payer for player transactions (partial-sign pattern),
so players need zero SOL. Players only need CHIP from `/faucet`.

## 7. Web app (`web/`)
- `/table` (projector): top-down oval table, 2–4 seats with name, short pubkey, on-chain balance,
  current bet, folded = greyed. Board cards animate in as they're confirmed. Pot. Whose turn.
  Winner banner with hand name ("Flush, Ace high"), chips-slide animation, and an Explorer link
  (`https://explorer.solana.com/tx/<sig>?cluster=devnet`) plus a QR code for it. A small corner shows "Live from camera" + a vision status dot.
- `/play/:seat` (phone): connect Phantom (devnet) **or** "Play as guest" (burner keypair stored in localStorage).
  Join → faucet → sign join tx. Big buttons Fold / Check / Call / Raise (slider + presets ½ pot, pot, all-in).
  Shows your stack, to-call amount, and a "your turn" vibration. Cash-out button between hands.
- `/dealer` (laptop): the current step's instruction text (section 1), live debug image of the rectified table
  with zone readings, low-confidence picker, overrides, Start / Confirm / Abort / Next hand,
  and a **Manual mode** toggle that skips vision entirely.
- `/calibrate`: marker status, capture the empty reference, zone editor.
- Serve phones over HTTPS (README: a `cloudflared` quick tunnel) because wallet in-app browsers require it.

## 8. Dev and demo
- `scripts/dev.sh` starts the server, chain service and web app; prints LAN + tunnel URLs.
- `DEMO.md`: setup checklist (mount, lock exposure, calibrate, fund the server wallet with devnet SOL **hours early**
  because the faucet is rate-limited, faucet test, one rehearsal hand) and the demo script.
- Fallback toggles available live: Manual card mode, `SETTLEMENT_MODE=custodial`.

## 9. Working rules for Claude Code
- Work one phase at a time (see the phase prompts). At the end of each phase run the tests, show how to run it, then stop.
- Prefer simple, readable code over cleverness. Type hints in Python, strict TS.
- Never commit keypairs, `.env`, videos, crops or models. Add them to `.gitignore`.
- If a library API is uncertain (pokerkit, Anchor version, wallet adapter), check the installed version's docs/source before writing code.
