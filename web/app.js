// AutoPoker table UI. Rendering (pixel cards, coin pile, seats, banners) is the teammate's design
// (vendor/pixel.js + vendor/style.css, copied unchanged); the data now comes from the game server:
//   WebSocket /ws/table  -> full public table state, pushed on every change
//   REST /api/auth/*     -> sign in at a seat (guest wallet signature + the seat's QR code)
//   REST /api/action     -> fold / check / call / raise (seat comes from the signed session token)
import { renderCard, renderBack, renderPile, renderAvatar, renderChip, spinCoin, cardLabel, parseCard } from './vendor/pixel.js';

const $ = (s) => document.querySelector(s);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const short = (a) => (a ? `${a.slice(0, 4)}…${a.slice(-4)}` : '');
const params = new URLSearchParams(location.search);
const SEAT_PARAM = Number(params.get('seat')) || 0;
const CODE_PARAM = params.get('code') || '';
const DEALER = params.has('dealer');
let dealerToken = (() => { try { return localStorage.getItem('pokervis.dealer'); } catch { return null; } })();
document.body.classList.toggle('mode-dealer', DEALER);
const STREETS = { 3: 'THE FLOP!', 4: 'THE TURN!', 5: 'THE RIVER!' };

// ---------------------------------------------------------------------------
// Guest wallet + session. The phone keeps an ed25519 keypair in localStorage and proves it owns it by
// signing a one-time challenge; the server answers with a token that is bound to (wallet, seat).
// ---------------------------------------------------------------------------
const ALPHA = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
const b58 = (bytes) => {
  const d = [0];
  for (const b of bytes) {
    let c = b;
    for (let j = 0; j < d.length; j++) { c += d[j] << 8; d[j] = c % 58; c = (c / 58) | 0; }
    while (c) { d.push(c % 58); c = (c / 58) | 0; }
  }
  let lead = '';
  for (const b of bytes) { if (b) break; lead += '1'; }
  return lead + d.reverse().map((x) => ALPHA[x]).join('');
};
const b64 = (bytes) => btoa(String.fromCharCode(...bytes));
const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

function loadWallet() {
  try {
    let sk = localStorage.getItem('pokervis.sk');
    if (!sk) { sk = b64(nacl.sign.keyPair().secretKey); localStorage.setItem('pokervis.sk', sk); }
    const kp = nacl.sign.keyPair.fromSecretKey(unb64(sk));
    return { kp, pub: b58(kp.publicKey) };
  } catch (e) { console.warn('wallet unavailable', e); return null; }
}
const W = loadWallet();

const SKEY = 'pokervis.session';
let session = (() => { try { return JSON.parse(localStorage.getItem(SKEY)); } catch { return null; } })();
function saveSession(s) {
  session = s;
  try { if (s) localStorage.setItem(SKEY, JSON.stringify(s)); else localStorage.removeItem(SKEY); } catch { /* storage blocked */ }
}

let reauthing = false;
async function api(path, body) {
  const out = await apiOnce(path, body);
  if (out.status === 401 && session && session.code && !reauthing && !path.startsWith('/api/auth/')) {
    reauthing = true;                                  // token expired or lost: sign in again with the wallet, then retry once
    try { await signIn(session.seat, session.code, savedName()); return unwrap(await apiOnce(path, body)); }
    catch { saveSession(null); } finally { reauthing = false; }
  }
  return unwrap(out);
}
function unwrap(out) {
  if (!out.ok) throw new Error(out.j.detail || out.statusText);
  return out.j;
}
async function apiOnce(path, body) {
  const r = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', ...(session && session.token ? { Authorization: `Bearer ${session.token}` } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, statusText: r.statusText, j };
}
const savedName = () => { try { return localStorage.getItem('pokervis.name') || ''; } catch { return ''; } };

/** Prove we own the guest wallet (sign a one-time challenge), take/keep the seat, remember the session. */
async function signIn(seat, code, name) {
  if (!W) throw new Error('This browser cannot make a wallet');
  const ch = await api('/api/auth/challenge', { pubkey: W.pub });
  const sig = nacl.sign.detached(new TextEncoder().encode(ch.message), W.kp.secretKey);
  const r = await api('/api/auth/login', { pubkey: W.pub, nonce: ch.nonce, signature: b64(sig), seat, seat_code: code, name });
  saveSession({ seat, token: r.token, code, at: Date.now() });   // the seat code is kept so a lost token can be renewed silently
  return r;
}

// ---------------------------------------------------------------------------
// Board (the card reveal animation is the teammate's: AI scan lock-on, then a flip)
// ---------------------------------------------------------------------------
const slots = [...document.querySelectorAll('.slot')].map((el) => ({
  el, canvas: el.querySelector('canvas'), label: el.querySelector('.label'), code: null, token: 0,
}));

function clearSlot(s) {
  s.code = null; s.token++;
  s.el.className = 'slot empty';
  s.label.textContent = '';
  s.canvas.getContext('2d').clearRect(0, 0, s.canvas.width, s.canvas.height);
}

async function revealSlot(s, code, delay, animate) {
  const token = ++s.token;
  s.code = code;
  if (animate) {
    await sleep(delay);
    if (token !== s.token) return;
    renderBack(s.canvas);
    s.el.className = 'slot scanning';
    s.label.textContent = 'SCANNING';
    await sleep(650);
    if (token !== s.token) return;
    s.el.classList.add('flip-out');
    await sleep(160);
    if (token !== s.token) return;
  }
  renderCard(s.canvas, code);
  s.el.className = animate ? 'slot revealed flip-in' : 'slot revealed';
  const p = parseCard(code);
  const text = cardLabel(code);
  s.label.innerHTML = '';
  s.label.append(text.slice(0, -1));
  const suit = document.createElement('i');
  suit.textContent = text.slice(-1);
  s.label.append(suit);
  s.label.dataset.suit = p ? p.suit : '';
}

function setBoard(cards, animate) {
  // A card keeps its slot for as long as it stays on the table, so adding a card never redraws the others.
  // Gone cards clear; new cards take the first free slot.
  slots.forEach((s) => { if (s.code && !cards.includes(s.code)) clearSlot(s); });
  let stagger = 0;
  for (const code of cards) {
    if (slots.some((s) => s.code === code)) continue;
    const free = slots.find((s) => !s.code);
    if (!free) break;
    revealSlot(free, code, stagger, animate);
    stagger += 300;
  }
}

let bannerTimer;
function banner(text) {
  const b = $('#banner');
  b.textContent = text;
  b.classList.remove('show');
  void b.offsetWidth;
  b.classList.add('show');
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(() => b.classList.remove('show'), 1800);
}

function setPill(id, state, text) {
  const el = $(id);
  el.dataset.state = state;
  el.querySelector('b').textContent = text;
}

// ---------------------------------------------------------------------------
// Seats: fixed positions (seat 1 bottom, 2 right, 3 top, 4 left), matching the physical table
// ---------------------------------------------------------------------------
const SEAT_ANGLE = { 1: Math.PI / 2, 2: 0, 3: -Math.PI / 2, 4: Math.PI };
const seatPos = (n) => ({ left: 50 + 55 * Math.cos(SEAT_ANGLE[n]), top: 50 + 60 * Math.sin(SEAT_ANGLE[n]) });   // top/bottom seats sit close to the rim so the table fits the screen
const seatEls = {};

function createSeat(n) {
  const el = document.createElement('div');
  el.className = 'seat';
  el.innerHTML = `
    <div class="flag" hidden></div>
    <canvas class="px avatar"></canvas>
    <img class="qr" alt="" hidden />
    <div class="name"></div>
    <div class="wallet"></div>
    <div class="hand" hidden><canvas class="px c1"></canvas><canvas class="px c2"></canvas></div>
    <div class="stack"></div>
    <div class="bet"></div>`;
  const pos = seatPos(n);
  el.style.left = `${pos.left}%`;
  el.style.top = `${pos.top}%`;
  $('#seats').appendChild(el);
  seatEls[n] = el;
  return el;
}

const badges = {};
function setupBadges() {
  const defs = { dealer: ['D', '#f4ecd8'], sb: ['SB', '#4cc9f0'], bb: ['BB', '#ff6b6b'] };
  for (const [key, [text, color]] of Object.entries(defs)) {
    const c = document.createElement('canvas');
    c.className = `px badge badge-${key}`;
    renderChip(c, text, color);
    c.hidden = true;
    $('#oval').appendChild(c);
    badges[key] = c;
  }
}

function placeBadges(roles) {
  for (const k of Object.keys(badges)) badges[k].hidden = !(roles && roles[k]);
  if (!roles || !roles.dealer) return;
  const place = (el, seat, nudge) => {
    const a = SEAT_ANGLE[seat] + nudge;
    el.style.left = `${50 + 39 * Math.cos(a)}%`;
    el.style.top = `${50 + 32 * Math.sin(a)}%`;     // vertical radius is smaller so the chips clear the top/bottom seats
  };
  place(badges.dealer, roles.dealer, -0.32);
  place(badges.sb, roles.sb, roles.sb === roles.dealer ? 0.32 : 0);
  place(badges.bb, roles.bb, 0);
}

function renderHand(el, seat, S) {
  const hand = el.querySelector('.hand');
  const [c1, c2] = hand.querySelectorAll('canvas');
  const shown = (S.showdown_cards || {})[seat.seat];
  const inPlay = seat.occupied && !seat.folded && ['betting', 'need_board', 'showdown'].includes(S.phase);
  if (shown && shown.length) {
    hand.hidden = false;
    [c1, c2].forEach((c, i) => {
      c.classList.toggle('ghost', !shown[i]);
      if (shown[i]) renderCard(c, shown[i]); else renderBack(c);
    });
  } else if (inPlay && S.phase !== 'showdown') {
    hand.hidden = false;                       // two face-down cards: a player is in this hand (nothing is read)
    [c1, c2].forEach((c) => { c.classList.remove('ghost'); renderBack(c); });
  } else if (inPlay) {
    hand.hidden = false;                       // showdown, nothing placed yet: dashed placeholders
    [c1, c2].forEach((c) => { c.classList.add('ghost'); renderBack(c); });
  } else {
    hand.hidden = true;
  }
}

function renderSeats(S) {
  const winners = S.result ? S.result.winners : [];
  for (const seat of S.seats) {
    const el = seatEls[seat.seat] || createSeat(seat.seat);
    const mine = session && session.seat === seat.seat && W && seat.pubkey === W.pub;
    el.classList.toggle('empty-seat', !seat.occupied);
    el.classList.toggle('me', !!mine);
    el.classList.toggle('turn', !!seat.to_act);
    el.classList.toggle('folded', !!seat.folded);
    el.classList.toggle('winner', winners.includes(seat.seat));
    el.classList.toggle('is-dealer', (seat.role || '').includes('D'));
    const qr = el.querySelector('img.qr');
    const avatar = el.querySelector('canvas.avatar');
    if (!seat.occupied) {
      renderAvatar(avatar, `empty-${seat.seat}`);
      const showQr = DEALER && !!dealerToken;
      if (showQr && !(seat.seat in qrUrls)) fetchQr(seat.seat);       // a fresh code is fetched each time a seat frees up
      qr.hidden = !(showQr && qrUrls[seat.seat]);
      if (!qr.hidden && qr.getAttribute('src') !== qrUrls[seat.seat]) qr.src = qrUrls[seat.seat];
      avatar.hidden = !qr.hidden;
      el.querySelector('.name').textContent = `SEAT ${seat.seat}`;
      el.querySelector('.wallet').textContent = '';
      el.querySelector('.stack').textContent = '';
      const bet = el.querySelector('.bet');
      bet.textContent = showQr ? 'SCAN TO JOIN' : SEAT_PARAM === seat.seat && !session ? 'THIS IS YOURS' : 'FREE';
      bet.className = 'bet';
      el.querySelector('.hand').hidden = true;
      el.querySelector('.flag').hidden = true;
      continue;
    }
    qr.hidden = true;
    avatar.hidden = false;
    delete qrUrls[seat.seat];
    if (el.dataset.pk !== seat.pubkey) { renderAvatar(el.querySelector('canvas.avatar'), seat.pubkey); el.dataset.pk = seat.pubkey; }
    el.querySelector('.name').textContent = (mine ? 'YOU · ' : '') + seat.name.toUpperCase();
    el.querySelector('.wallet').textContent = short(seat.pubkey);
    el.querySelector('.stack').textContent = `STACK ${seat.stack}`;

    const bet = el.querySelector('.bet');
    const gain = S.result && S.result.deltas_by_seat ? S.result.deltas_by_seat[seat.seat] : 0;
    if (S.result && winners.includes(seat.seat)) { bet.textContent = `WIN +${gain}`; bet.className = 'bet win'; }
    else if (seat.last_action) { bet.textContent = seat.last_action; bet.className = 'bet act'; }
    else if (seat.bet > 0) { bet.textContent = `BET ${seat.bet}`; bet.className = 'bet act'; }
    else { bet.textContent = '—'; bet.className = 'bet'; }

    const inHand = ['betting', 'need_board', 'showdown'].includes(S.phase);
    let flag = '';
    if (seat.to_act) flag = 'TO ACT';
    else if (seat.folded) flag = 'FOLDED';
    else if (inHand && seat.stack === 0 && seat.bet > 0) flag = 'ALL IN!';
    else if (!inHand && seat.stack === 0) flag = 'OUT: REBUY';
    const f = el.querySelector('.flag');
    f.hidden = !flag;
    f.textContent = flag;
    renderHand(el, seat, S);
  }
  placeBadges(S.roles);
}

// ---------------------------------------------------------------------------
// Pot, coins
// ---------------------------------------------------------------------------
function renderPot(S) {
  $('#pot-amount').textContent = S.pot;
  const per = Math.max(1, S.blinds[1]);
  renderPile($('#pile'), Math.min(49, Math.ceil(S.pot / per)));
}

function flyCoins(seatNo, dir, n) {
  const oval = $('#oval');
  const p = seatPos(seatNo);
  const seat = { left: `${p.left}%`, top: `${p.top}%` };
  const pot = { left: '50%', top: '63%' };                           // where the pot sits now
  const [from, to] = dir === 'in' ? [seat, pot] : [pot, seat];
  for (let i = 0; i < n; i++) {
    setTimeout(() => {
      const c = document.createElement('canvas');
      c.className = 'fly';
      Object.assign(c.style, from);
      const stop = spinCoin(c, 10, 14);
      oval.appendChild(c);
      void c.offsetWidth;
      Object.assign(c.style, to);
      setTimeout(() => { stop(); c.remove(); }, 650);
    }, i * 110);
  }
}

// ---------------------------------------------------------------------------
// Status bar: one plain sentence about what is going on, for everyone
// ---------------------------------------------------------------------------
function myTurn(S) {
  return !!(session && S.to_act === session.seat && S.phase === 'betting');
}

/** Whatever the camera sees on the board, always. Only if the camera is offline do we show the game's own board. */
function boardOnTable(S) {
  const camLive = S.vision && S.vision.age != null && S.vision.age < 10;
  return (camLive ? (S.board_camera || []) : S.board).slice(0, 5);
}

/** Dealer only: the round never starts by itself. Explains why when it cannot start yet. */
function renderStart(S) {
  const show = DEALER && !!dealerToken && S.phase === 'waiting';
  $('#start-box').hidden = !show;
  $('#status').classList.toggle('with-start', show);
  if (!show) return;
  const ok = !!(S.can_start && S.can_start.ok);
  $('#start-round').disabled = !ok;
  $('#start-round').textContent = ok ? 'START ROUND?' : 'NOT READY';
  $('#start-err').textContent = ok ? '' : String((S.can_start && S.can_start.reason) || '').toUpperCase();
}

function renderStatus(S) {
  const st = S.status || { code: 'wait', headline: '', detail: '' };
  const box = $('#status');
  let head = st.headline;
  let detail = st.detail;
  const m = meSeat(S);
  if (myTurn(S)) {
    head = 'YOUR TURN!';
    const call = toCall(S, m);
    detail = `${call ? `CALL ${Math.min(call, m.stack)}` : 'CHECK'}, RAISE OR FOLD · POT ${S.pot}`;
  } else if (m && st.code === 'showdown' && !m.folded && (S.showdown_cards[m.seat] || []).length < 2) {
    head = 'YOUR CARDS, PLEASE!';
    detail = 'Put BOTH of your cards face up in your area on the table.';
  }
  box.dataset.code = st.code;
  box.classList.toggle('mine', myTurn(S));
  box.classList.toggle('err', !!S.settle_error);
  $('#status-headline').textContent = head || '…';
  if (DEALER) {                                   // the dealer also needs to hear about camera trouble and card conflicts
    const extra = [...(S.vision && S.vision.warnings ? S.vision.warnings : []), ...(S.prompts || [])];
    if (extra.length) detail = `${detail ? detail + ' · ' : ''}${extra.join(' · ')}`;
  }
  $('#status-detail').textContent = detail || '';
  const out = boardOnTable(S).length;
  const street = out >= 5 ? 'RIVER' : out === 4 ? 'TURN' : out === 3 ? 'FLOP' : 'PRE-FLOP';
  $('#street').textContent = S.phase === 'waiting' ? 'WAITING' : street;
  $('#blinds').textContent = `BLINDS ${S.blinds[0]}/${S.blinds[1]} · BUY-IN ${S.buy_in}`;
}

// ---------------------------------------------------------------------------
// Your controls
// ---------------------------------------------------------------------------
function meSeat(S) {
  if (!S || !session || !W) return null;
  return S.seats.find((x) => x.occupied && x.seat === session.seat && x.pubkey === W.pub) || null;
}
function toCall(S, m) {
  const maxBet = Math.max(0, ...S.seats.filter((x) => x.occupied).map((x) => x.bet || 0));
  return Math.max(0, maxBet - (m ? m.bet : 0));
}

let raiseOpen = false;
let lastAvatar = '';
function renderControls(S) {
  const m = meSeat(S);
  $('#controls').hidden = !m;
  if (!m) return;
  if (lastAvatar !== m.pubkey) { renderAvatar($('#me-avatar'), m.pubkey); lastAvatar = m.pubkey; }
  const turn = myTurn(S);
  const call = Math.min(toCall(S, m), m.stack);
  $('#me-name').textContent = m.name.toUpperCase();
  $('#me-stack').textContent = `STACK ${m.stack}`;
  let hint;
  if (turn) hint = call ? `${call} TO CALL` : 'NOTHING TO CALL';
  else if (S.phase === 'betting' && S.to_act) hint = `WAITING FOR ${S.seats[S.to_act - 1].name.toUpperCase()}`;
  else hint = (S.status && S.status.headline) || '';
  $('#me-hint').textContent = m.folded ? 'YOU FOLDED' : hint;
  $('#b-fold').disabled = !turn;
  const cb = $('#b-call');
  cb.disabled = !turn;
  cb.textContent = call === 0 ? 'CHECK' : call >= m.stack ? 'CALL ALL-IN' : `CALL ${call}`;
  const canRaise = turn && S.min_raise_to != null;
  $('#b-raise').disabled = !canRaise;
  if (!canRaise) raiseOpen = false;
  $('#raise-box').hidden = !raiseOpen;
  if (raiseOpen && canRaise) {
    const r = $('#raise');
    r.min = S.min_raise_to; r.max = S.max_raise_to;
    if (+r.value < +r.min || +r.value > +r.max) r.value = S.min_raise_to;
    $('#raise-val').textContent = r.value;
  }
  let rebuy = $('#b-rebuy');
  if (!rebuy) {
    rebuy = document.createElement('button');
    rebuy.id = 'b-rebuy'; rebuy.type = 'button'; rebuy.className = 'go wide';
    rebuy.onclick = async () => { try { await api('/api/rebuy', {}); } catch (e) { $('#act-err').textContent = e.message; } };
    $('#b-leave').before(rebuy);
  }
  rebuy.hidden = !(m.stack < S.buy_in && S.phase === 'waiting');      // buy in between rounds, while the dealer has not started yet
  rebuy.textContent = m.stack === 0 ? `REBUY ${S.buy_in} CHIPS` : `TOP UP TO ${S.buy_in}`;
}

async function act(type, amount) {
  $('#act-err').textContent = '';
  try { await api('/api/action', { type, amount }); raiseOpen = false; }
  catch (e) { $('#act-err').textContent = e.message.toUpperCase(); }
}

function setupControls() {
  $('#b-fold').onclick = () => act('fold');
  $('#b-call').onclick = () => act($('#b-call').textContent === 'CHECK' ? 'check' : 'call');
  $('#b-raise').onclick = () => { raiseOpen = !raiseOpen; if (S) renderControls(S); };
  $('#raise').oninput = () => { $('#raise-val').textContent = $('#raise').value; };
  $('#b-raise-go').onclick = () => act('raise', Number($('#raise').value));
  const preset = (frac) => () => {
    if (!S) return;
    const m = meSeat(S);
    const call = toCall(S, m);
    const target = m.bet + call + frac * (S.pot + call);
    const r = $('#raise');
    r.value = Math.max(+r.min, Math.min(+r.max, Math.round(target)));
    $('#raise-val').textContent = r.value;
  };
  $('#p-half').onclick = preset(0.5);
  $('#p-pot').onclick = preset(1);
  $('#p-all').onclick = () => { const r = $('#raise'); r.value = r.max; $('#raise-val').textContent = r.value; };
  $('#b-leave').onclick = async () => {
    if (!confirm('Leave the table and cash out your chips? You will need a new QR code to sit down again.')) return;
    try { await api('/api/cashout', {}); saveSession(null); location.href = location.pathname; }
    catch (e) { $('#act-err').textContent = e.message.toUpperCase(); }
  };
}

// ---------------------------------------------------------------------------
// Sit down (opened from a seat's QR code)
// ---------------------------------------------------------------------------
function setupJoin() {
  $('#join-seat').textContent = SEAT_PARAM || '';
  $('#join-wallet').textContent = W ? short(W.pub) : 'UNAVAILABLE';
  $('#join-name').value = (() => { try { return localStorage.getItem('pokervis.name') || ''; } catch { return ''; } })();
  $('#b-sit').onclick = async () => {
    $('#join-err').textContent = '';
    try {
      const name = $('#join-name').value.trim();
      try { localStorage.setItem('pokervis.name', name); } catch { /* storage blocked */ }
      await signIn(SEAT_PARAM, CODE_PARAM, name);
      history.replaceState(null, '', location.pathname);       // drop the seat code from the address bar
      if (S) render(S, false);
    } catch (e) { $('#join-err').textContent = e.message.toUpperCase(); }
  };
}

let autoTried = false;
/** Scanning a seat QR should just work: if this wallet already owns the seat, or it is free and we know the name, sign in. */
function maybeAutoJoin(S) {
  if (autoTried || !W || meSeat(S) || !(SEAT_PARAM > 0 && CODE_PARAM)) return;
  const seat = S.seats[SEAT_PARAM - 1];
  if (!seat) return;
  const mine = seat.occupied && seat.pubkey === W.pub;
  const freeAndKnown = !seat.occupied && !!savedName() && ['waiting', 'done'].includes(S.phase);
  if (!mine && !freeAndKnown) return;
  autoTried = true;
  signIn(SEAT_PARAM, CODE_PARAM, savedName())
    .then(() => { history.replaceState(null, '', location.pathname); if (S) render(S, false); })
    .catch((e) => { autoTried = false; $('#join-err').textContent = String(e.message).toUpperCase(); });
}

function renderJoin(S) {
  maybeAutoJoin(S);
  const show = !meSeat(S) && SEAT_PARAM > 0 && !!CODE_PARAM;
  $('#join').hidden = !show;
  if (!show) return;
  const seat = S.seats[SEAT_PARAM - 1];
  if (seat && seat.occupied && !(session && W && seat.pubkey === W.pub)) {
    $('#join-err').textContent = 'THAT SEAT IS TAKEN. ASK THE DEALER FOR A FRESH QR.';
    $('#b-sit').disabled = true;
  } else {
    $('#b-sit').disabled = S.phase !== 'waiting' && S.phase !== 'done' && !(seat && seat.occupied);
    if ($('#b-sit').disabled) $('#join-err').textContent = 'A HAND IS IN PROGRESS. YOU CAN SIT DOWN BETWEEN HANDS.';
  }
}

// ---------------------------------------------------------------------------
// State -> screen
// ---------------------------------------------------------------------------
let S = null;
let lastBets = {};
let lastResultKey = '';
let lastHandBanner = null;

function render(state, animate) {
  const old = S;
  S = state;
  // a saved session that the server no longer knows (restart, cash-out elsewhere) is dropped
  // (not within 8 s of signing in: the snapshot we hold may be older than the login itself)
  if (session && !meSeat(S) && Date.now() - (session.at || 0) > 8000 && S.seats[session.seat - 1] && !S.seats[session.seat - 1].occupied) saveSession(null);

  document.body.classList.toggle('is-player', !DEALER && !!session);   // players can see the real cards: no board on phones
  setPill('#net-status', 'live', S.mode === 'mock' ? 'MOCK' : 'LIVE');
  const camOk = S.vision && S.vision.age != null && S.vision.age < 5;
  setPill('#cam-status', camOk ? 'live' : S.manual_mode ? 'demo' : 'error', camOk ? 'LIVE' : S.manual_mode ? 'MANUAL' : 'OFFLINE');

  if (S.hand_id && S.hand_id !== lastHandBanner && S.phase !== 'waiting') {   // once per real hand, never for a failed start
    if (old && animate) banner('NEW HAND');
    lastHandBanner = S.hand_id;
  }
  const onTable = boardOnTable(S);                                     // the camera is the source of truth for what is out
  const onTableOld = old ? boardOnTable(old).length : 0;
  if (old && animate && onTable.length > onTableOld && STREETS[Math.min(5, onTable.length)]) banner(STREETS[Math.min(5, onTable.length)]);
  setBoard(onTable, animate);

  if (animate) {
    for (const seat of S.seats) {
      if (!seat.occupied) continue;
      const before = lastBets[seat.seat] || 0;
      if (seat.bet > before) flyCoins(seat.seat, 'in', Math.max(1, Math.min(5, Math.round((seat.bet - before) / Math.max(1, S.blinds[1])))));
    }
    if (old && myTurn(S) && !myTurn(old)) { banner('YOUR TURN!'); if (navigator.vibrate) navigator.vibrate(200); }
  }
  lastBets = Object.fromEntries(S.seats.filter((x) => x.occupied).map((x) => [x.seat, x.bet || 0]));

  const key = S.result ? `${S.hand_id}:${S.result.winners.join(',')}` : '';
  if (key && key !== lastResultKey && animate) {
    const names = S.result.winners.map((w) => S.seats[w - 1].name.toUpperCase()).join(' & ');
    setTimeout(() => banner(`${names} WIN${S.result.winners.length === 1 ? 'S' : ''}!`), 400);
    S.result.winners.forEach((w) => flyCoins(w, 'out', 6));
  }
  lastResultKey = key;

  renderSeats(S);
  renderPot(S);
  renderStatus(S);
  renderStart(S);
  renderControls(S);
  renderJoin(S);
}

// ---------------------------------------------------------------------------
// Connection
// ---------------------------------------------------------------------------
let first = true;
function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/table`);
  ws.onopen = () => setPill('#net-status', 'live', 'LIVE');
  ws.onmessage = (e) => {
    try { render(JSON.parse(e.data), !first); first = false; } catch (err) { console.warn('bad state', err); }
  };
  ws.onclose = () => { setPill('#net-status', 'error', 'OFFLINE'); setTimeout(connect, 1500); };
  ws.onerror = () => ws.close();
}

// ---------------------------------------------------------------------------
// Dealer mode (/dealer): the same screen as the table plus a PIN and a tools drawer. The game itself is fully
// automatic (hands start, cards are accepted, payouts and the next hand happen by themselves); the drawer holds
// the escape hatches for when the camera is wrong.
// ---------------------------------------------------------------------------
const qrUrls = {};
async function dapi(path, body) {
  const r = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${dealerToken}` },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (r.status === 401) { dealerToken = null; try { localStorage.removeItem('pokervis.dealer'); } catch { /* */ } showDealerLogin(); }
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}
async function dblob(path) {
  const r = await fetch(path, { headers: { Authorization: `Bearer ${dealerToken}` } });
  if (!r.ok) throw new Error(String(r.status));
  return URL.createObjectURL(await r.blob());
}
async function fetchQr(n) {
  qrUrls[n] = '';
  try { qrUrls[n] = await dblob(`/api/dealer/seat_qr/${n}.png`); if (S) renderSeats(S); } catch { delete qrUrls[n]; }
}
function showDealerLogin() { $('#dealer-login').hidden = false; $('#dealer-tools').hidden = true; $('#qr-btn').hidden = true; }

/** Every seat's QR (occupied or not) so anyone can sign back in after a device problem. */
async function openQrDialog() {
  const dlg = $('#qr-dialog');
  const grid = $('#qr-grid');
  grid.innerHTML = '';
  const seats = S ? S.seats : [1, 2, 3, 4].map((n) => ({ seat: n, occupied: false }));
  for (const seat of seats) {
    const fig = document.createElement('figure');
    fig.innerHTML = `<img alt="QR for seat ${seat.seat}" /><figcaption>SEAT ${seat.seat}<small>${seat.occupied ? seat.name.toUpperCase() : 'FREE'}</small></figcaption>`;
    grid.appendChild(fig);
    dblob(`/api/dealer/seat_qr/${seat.seat}.png`).then((u) => { fig.querySelector('img').src = u; }).catch(() => { fig.querySelector('img').alt = 'could not load'; });
  }
  if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
}

let camBlob = '';
async function dealerPoll() {
  if (!dealerToken) return;
  try {
    const d = await dapi('/api/dealer/state');
    const reads = Object.entries(d.readings || {}).sort().map(([z, r]) => `${z}: ${cardLabel(r.code)} ${Math.round(r.conf * 100)}%${r.stable ? '' : ' (UNSTABLE)'}`);
    $('#cam-reads').textContent = reads.length ? `READS ${reads.join(' · ')}` : 'READS NO CARDS';
    if ($('#dealer-tools').open) {
      const u = await dblob('/api/vision/frame.jpg').catch(() => '');
      if (u) { $('#cam-img').src = u; if (camBlob) URL.revokeObjectURL(camBlob); camBlob = u; }
    }
  } catch { /* transient */ }
}

function setupDealer() {
  if (!DEALER) return;
  $('#join').hidden = true;
  const start = () => { $('#dealer-login').hidden = true; $('#dealer-tools').hidden = false; $('#qr-btn').hidden = false; dealerPoll(); setInterval(dealerPoll, 1500); };
  $('#qr-btn').onclick = openQrDialog;
  $('#qr-close').onclick = () => { const d = $('#qr-dialog'); if (d.close) d.close(); else d.removeAttribute('open'); };
  $('#d-go').onclick = async () => {
    $('#d-err').textContent = '';
    try {
      const r = await fetch('/api/dealer/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pin: $('#d-pin').value }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || 'WRONG PIN');
      dealerToken = j.token;
      try { localStorage.setItem('pokervis.dealer', dealerToken); } catch { /* */ }
      start();
      if (S) renderSeats(S);
    } catch (e) { $('#d-err').textContent = e.message.toUpperCase(); }
  };
  $('#start-round').onclick = async () => {
    $('#start-err').textContent = '';
    try { await dapi('/api/dealer/start_round', {}); }
    catch (e) { $('#start-err').textContent = e.message.toUpperCase(); }
  };
  $('#t-abort').onclick = async () => {
    $('#t-err').textContent = '';
    if (!confirm('Reset this hand? Nobody wins or loses chips.')) return;
    try { await dapi('/api/dealer/abort_hand', {}); } catch (e) { $('#t-err').textContent = e.message.toUpperCase(); }
  };
  if (dealerToken) start(); else showDealerLogin();
}

spinCoin($('#logo-coin'), 10, 8);
setupDealer();
setupBadges();
setupControls();
setupJoin();
connect();
