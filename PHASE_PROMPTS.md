# Claude Code prompts, one per phase

Put `CLAUDE.md` in the root of an empty repo first. Claude Code reads it automatically.
Paste one prompt at a time and get each phase working before moving on.
Phases 1–3 (vision), 4 (game + web) and 5 (chain) are independent. With several laptops,
run them in parallel in separate branches; the contracts in CLAUDE.md section 3 keep them compatible.

---

## Phase 0: Scaffold
```
Read CLAUDE.md fully. Scaffold the repo layout from section 2: Python package with pyproject
(or requirements.txt), web app with Vite+React+TS+Tailwind, chain/ with an Anchor workspace, .gitignore,
and scripts/dev.sh. Then write tools/make_mat.py, which generates a printable PDF: 4 ArUco markers
(DICT_4X4_50, IDs 0-3, 8 cm with the ID printed under each) on separate A4 pages, plus one A4 page
showing the zone layout from section 1 with dimensions in cm so we can tape the zones.
Run it and show me where the PDF is. Stop when done.
```

## Phase 1: Camera, calibration, card detection
```
Implement CLAUDE.md sections 4.1–4.3: vision/camera.py, vision/calibrate.py (CLI version first:
detect markers, compute homography, capture the empty-table reference, default zones.json from the mat
layout), and vision/detect.py with the --debug viewer. Add unit tests for detect.py using synthetic
images: draw white 63x88mm rectangles with random rotation onto a green background at 2 px/mm,
include an overlapping pair and a hand-shaped blob, and check that the detector finds the right cards,
warps to 224x320 portrait, flags OVERLAP, and rejects the blob.
Give me exact commands to try it live with my phone as a webcam. Stop when done.
```

## Phase 2: Dataset from videos + training
```
Implement CLAUDE.md sections 4.4, 4.6 and 4.7: training/extract_crops.py (with contact sheets
and dedupe), training/build_gallery.py (kNN baseline), training/train.py, training/eval.py,
training/eval_multi.py and vision/classify.py with 180-degree TTA. Make every script runnable
with sensible defaults and a --help. Pick the device automatically (cuda/mps/cpu).
Then write data/RECORDING.md: the exact instructions for recording the 108 videos
(52 cards x train/val + BACK + NONE), using the real overhead mount.
Test the scripts end-to-end on a tiny fake dataset you generate synthetically, so I know they
run before we record. Stop when done.
```
After this phase, the humans record the videos, then run:
```
python -m training.extract_crops && open data/crops/_sheets   # eyeball every sheet
python -m training.build_gallery && python -m training.eval --knn
python -m training.train && python -m training.eval && python -m training.eval_multi
```
Paste the eval output back to Claude Code if accuracy is below target and ask it to diagnose.

## Phase 3: Tracker + live vision service
```
Implement CLAUDE.md section 4.5 (vision/tracker.py: voting, motion gate, uniqueness via
linear_sum_assignment, hard-example saving) and vision/service.py, which runs the loop and publishes the
section 3 vision message. Update the --debug viewer to draw the code + confidence on each card, and
yellow for not-stable / red for conflicts. Add tests for the tracker (voting, uniqueness conflict
resolution, already-used cards). Stop when done.
```

## Phase 4: Game engine + server + web (manual card entry, no camera)
```
Implement CLAUDE.md section 5 (game/engine.py with the listed tests), server/app.py with the
section 3 REST + WebSocket contracts, and the web app pages /table, /play/:seat and /dealer from
section 7. For now the chain is mocked: an in-memory ledger with the same interface as the chain service.
The dealer console must allow entering every card manually. Target: we can play a full
4-player hand from phones on the LAN with manual card entry and see balances update on /table.
Make /table look great on a projector (dark felt, large type, smooth animations).
Stop when done and tell me how to run it.
```

## Phase 5: Solana program + chain service
```
Implement CLAUDE.md section 6: the Anchor program with all listed tests passing on a local
validator, chain/scripts/setup-devnet.ts, and chain/service with every endpoint in section 3, including
fee-payer partial signing for player transactions and SETTLEMENT_MODE=custodial fallback.
Deploy to devnet, run setup-devnet, and do a scripted end-to-end check: 2 test wallets get
faucet CHIP, join, begin_hand, settle with deltas {+50,-50}, cash out. Print the Explorer links.
If devnet airdrop is rate-limited, tell me exactly what to do (faucet website) instead of retrying in a loop.
Stop when done.
```

## Phase 6: Integration
```
Wire everything together: replace the mock ledger with the chain service, connect the vision
service to the engine's *_READ states, add wallet connect + guest burner wallet to /play, add
the low-confidence picker and live debug image to /dealer, and add showdown_hash computation.
Add the /calibrate page. Keep Manual mode and custodial mode as live toggles.
Run one full hand in manual mode with real devnet settlement and show me the Explorer link.
Stop when done.
```

## Phase 7: Demo hardening
```
Write DEMO.md (setup checklist + 3-minute script per CLAUDE.md section 8). Add: a reconnect
banner if any service drops, a "Rehearsal" button on /dealer that replays the last hand's
animations, sound effects for card read / win, and a QR code on /table that opens /play for
the next free seat. Review the whole codebase for crash risks during a live demo (unhandled
exceptions, websocket reconnects, camera disconnects) and fix them. Stop when done.
```
