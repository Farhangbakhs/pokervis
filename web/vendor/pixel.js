// Pixel-art renderers: cards, card backs and gold coins.
// Everything is drawn at tiny logical resolution and scaled up by CSS
// (image-rendering: pixelated) for the chunky retro look.

const PX = {
  ink: '#1a1a2e',
  cream: '#f4ecd8',
  creamShade: '#d9cfb5',
  red: '#d8323c',
  black: '#1a1a2e',
  gold: '#ffc93c',
  goldLight: '#fff1a8',
  goldDark: '#d99a1e',
  goldOutline: '#6b3d00',
  purple: '#3b1f6b',
  teal: '#14f195',
};

// 3x5 font for ranks
const FONT = {
  A: ['010', '101', '111', '101', '101'],
  2: ['110', '001', '010', '100', '111'],
  3: ['110', '001', '010', '001', '110'],
  4: ['101', '101', '111', '001', '001'],
  5: ['111', '100', '110', '001', '110'],
  6: ['011', '100', '110', '101', '010'],
  7: ['111', '001', '010', '010', '010'],
  8: ['010', '101', '010', '101', '010'],
  9: ['010', '101', '011', '001', '110'],
  1: ['010', '110', '010', '010', '111'],
  0: ['010', '101', '101', '101', '010'],
  J: ['001', '001', '001', '101', '010'],
  Q: ['010', '101', '101', '110', '011'],
  K: ['101', '101', '110', '101', '101'],
  D: ['110', '101', '101', '101', '110'],
  S: ['011', '100', '010', '001', '110'],
  B: ['110', '101', '110', '101', '110'],
};

const SUIT_SMALL = {
  h: ['.X.X.', 'XXXXX', 'XXXXX', '.XXX.', '..X..'],
  d: ['..X..', '.XXX.', 'XXXXX', '.XXX.', '..X..'],
  s: ['..X..', '.XXX.', 'XXXXX', '..X..', '.XXX.'],
  c: ['..X..', '.XXX.', 'X.X.X', 'XXXXX', '..X..'],
};

const SUIT_BIG = {
  h: ['.XX.XX.', 'XXXXXXX', 'XXXXXXX', 'XXXXXXX', '.XXXXX.', '..XXX..', '...X...'],
  d: ['...X...', '..XXX..', '.XXXXX.', 'XXXXXXX', '.XXXXX.', '..XXX..', '...X...'],
  s: ['...X...', '..XXX..', '.XXXXX.', 'XXXXXXX', 'XX.X.XX', '...X...', '..XXX..'],
  c: ['..XXX..', '..XXX..', 'XX.X.XX', 'XXXXXXX', 'XX.X.XX', '...X...', '..XXX..'],
};

export const SUIT_SYMBOL = { h: '♥', d: '♦', s: '♠', c: '♣' };
const RANK_TEXT = { T: '10' };

export const CARD_W = 32;
export const CARD_H = 44;

function bitmap(ctx, rows, x0, y0, color, { scale = 1, rotate = false, W = 0, H = 0 } = {}) {
  ctx.fillStyle = color;
  rows.forEach((row, y) => {
    [...row].forEach((ch, x) => {
      if (ch !== '1' && ch !== 'X') return;
      let px = x0 + x * scale;
      let py = y0 + y * scale;
      if (rotate) {
        px = W - px - scale;
        py = H - py - scale;
      }
      ctx.fillRect(px, py, scale, scale);
    });
  });
}

function cardBase(ctx, fill) {
  const W = CARD_W, H = CARD_H;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = PX.ink;
  ctx.fillRect(1, 0, W - 2, H);
  ctx.fillRect(0, 1, W, H - 2);
  ctx.fillStyle = fill;
  ctx.fillRect(2, 1, W - 4, H - 2);
  ctx.fillRect(1, 2, W - 2, H - 4);
}

/** Parse "As", "Td", "10h" → { rank: 'A', suit: 's' } */
export function parseCard(code) {
  const c = String(code).trim();
  const suit = c.slice(-1).toLowerCase();
  let rank = c.slice(0, -1).toUpperCase();
  if (rank === '10') rank = 'T';
  if (!SUIT_BIG[suit] || !(rank === 'T' || FONT[rank])) return null;
  return { rank, suit };
}

export function cardLabel(code) {
  const p = parseCard(code);
  if (!p) return '??';
  return (RANK_TEXT[p.rank] || p.rank) + SUIT_SYMBOL[p.suit];
}

export function renderCard(canvas, code) {
  const p = parseCard(code);
  if (!p) return renderUnknown(canvas);
  canvas.width = CARD_W;
  canvas.height = CARD_H;
  const ctx = canvas.getContext('2d');
  const W = CARD_W, H = CARD_H;
  cardBase(ctx, PX.cream);
  // bottom shade for depth
  ctx.fillStyle = PX.creamShade;
  ctx.fillRect(2, H - 3, W - 4, 1);

  const color = p.suit === 'h' || p.suit === 'd' ? PX.red : PX.black;
  const text = RANK_TEXT[p.rank] || p.rank;
  const opts = { rotate: true, W, H };

  // top-left corner + mirrored bottom-right corner
  [...text].forEach((ch, i) => {
    bitmap(ctx, FONT[ch], 3 + i * 4, 3, color);
    bitmap(ctx, FONT[ch], 3 + i * 4, 3, color, opts);
  });
  bitmap(ctx, SUIT_SMALL[p.suit], 3, 10, color);
  bitmap(ctx, SUIT_SMALL[p.suit], 3, 10, color, opts);

  // big centre pip
  bitmap(ctx, SUIT_BIG[p.suit], (W - 14) / 2, (H - 14) / 2, color, { scale: 2 });

  // face cards get a little gold crown above the pip
  if ('JQK'.includes(p.rank)) {
    bitmap(ctx, ['X.X.X', 'XXXXX'], (W - 5) / 2, 10, PX.goldDark);
  }
}

export function renderBack(canvas) {
  canvas.width = CARD_W;
  canvas.height = CARD_H;
  const ctx = canvas.getContext('2d');
  const W = CARD_W, H = CARD_H;
  cardBase(ctx, PX.gold);
  ctx.fillStyle = PX.purple;
  ctx.fillRect(3, 3, W - 6, H - 6);
  ctx.fillStyle = PX.teal;
  for (let y = 3; y < H - 3; y++) {
    for (let x = 3; x < W - 3; x++) {
      if ((x + y) % 6 === 0 || (x - y + 60) % 6 === 0) ctx.fillRect(x, y, 1, 1);
    }
  }
  drawCoin(ctx, (W - 10) / 2, (H - 10) / 2, 10, 1);
}

export function renderUnknown(canvas) {
  canvas.width = CARD_W;
  canvas.height = CARD_H;
  const ctx = canvas.getContext('2d');
  cardBase(ctx, PX.cream);
  const q = ['111', '001', '010', '000', '010'];
  bitmap(ctx, q, (CARD_W - 9) / 2, (CARD_H - 15) / 2, PX.red, { scale: 3 });
}

/** Pixel gold coin. squash 0..1 narrows it horizontally (for spinning). */
export function drawCoin(ctx, ox, oy, size = 10, squash = 1) {
  const r = size / 2;
  for (let y = 0; y < size; y++) {
    const dy = y + 0.5 - r;
    const hw = Math.sqrt(Math.max(0, r * r - dy * dy)) * squash;
    const x0 = Math.round(ox + r - hw);
    const x1 = Math.max(x0 + 1, Math.round(ox + r + hw));
    ctx.fillStyle = PX.goldOutline;
    ctx.fillRect(x0, oy + y, x1 - x0, 1);
    if (y > 0 && y < size - 1 && x1 - x0 > 2) {
      ctx.fillStyle = y < size * 0.4 ? PX.goldLight : y > size * 0.7 ? PX.goldDark : PX.gold;
      ctx.fillRect(x0 + 1, oy + y, x1 - x0 - 2, 1);
    }
  }
  if (squash > 0.6) {
    ctx.fillStyle = PX.goldDark;
    ctx.fillRect(Math.round(ox + r) - 1, oy + 3, 1, size - 6);
  }
}

const SPIN = [1, 0.75, 0.4, 0.12, 0.4, 0.75];

/** Animate a spinning coin into a canvas element. Returns a stop function. */
export function spinCoin(canvas, size = 10, fps = 10) {
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  let f = 0;
  const id = setInterval(() => {
    ctx.clearRect(0, 0, size, size);
    drawCoin(ctx, 0, 0, size, SPIN[f++ % SPIN.length]);
  }, 1000 / fps);
  drawCoin(ctx, 0, 0, size, 1);
  return () => clearInterval(id);
}

// ---------------------------------------------------------------------------
// Avatars: a mirrored 5x5 pixel "space invader" generated from a wallet id,
// so every player gets a stable, unique little sprite.
// ---------------------------------------------------------------------------
const AVATAR_COLORS = ['#ffc93c', '#14f195', '#9945ff', '#ff6b6b', '#4cc9f0', '#f72585', '#ff9f1c', '#b5e48c'];

function seedRandom(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) h = Math.imul(h ^ str.charCodeAt(i), 16777619);
  return () => {
    h = (h + 0x6d2b79f5) | 0;
    let t = Math.imul(h ^ (h >>> 15), 1 | h);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function renderAvatar(canvas, seed) {
  const S = 9;
  canvas.width = S;
  canvas.height = S;
  const ctx = canvas.getContext('2d');
  const rand = seedRandom(seed);
  const color = AVATAR_COLORS[Math.floor(rand() * AVATAR_COLORS.length)];
  ctx.fillStyle = PX.ink;
  ctx.fillRect(0, 0, S, S);
  ctx.fillStyle = '#2a2448';
  ctx.fillRect(1, 1, S - 2, S - 2);
  ctx.fillStyle = color;
  for (let y = 0; y < 5; y++) {
    for (let x = 0; x < 3; x++) {
      if (rand() < 0.55) {
        ctx.fillRect(2 + x, 2 + y, 1, 1);
        ctx.fillRect(2 + 4 - x, 2 + y, 1, 1);
      }
    }
  }
  // eyes, so they always read as little characters
  ctx.fillStyle = PX.ink;
  ctx.fillRect(3, 3, 1, 1);
  ctx.fillRect(5, 3, 1, 1);
}

/** Pixel poker chip / dealer button with a short label ("D", "SB", "BB"). */
export function renderChip(canvas, text, fill, ink = PX.ink) {
  const S = 15;
  canvas.width = S;
  canvas.height = S;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, S, S);
  const r = S / 2;
  for (let y = 0; y < S; y++) {
    const dy = y + 0.5 - r;
    const hw = Math.sqrt(Math.max(0, r * r - dy * dy));
    const x0 = Math.round(r - hw), x1 = Math.round(r + hw);
    if (x1 <= x0) continue;
    ctx.fillStyle = PX.ink;
    ctx.fillRect(x0, y, x1 - x0, 1);
    if (y > 0 && y < S - 1 && x1 - x0 > 2) {
      ctx.fillStyle = fill;
      ctx.fillRect(x0 + 1, y, x1 - x0 - 2, 1);
      // chip edge stripes
      if (y % 4 === 1 && y > 1 && y < S - 2) {
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(x0 + 1, y, 1, 1);
        ctx.fillRect(x1 - 2, y, 1, 1);
      }
    }
  }
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(6, 1, 3, 1);
  ctx.fillRect(6, S - 2, 3, 1);
  const w = text.length * 4 - 1;
  [...text].forEach((ch, i) => bitmap(ctx, FONT[ch], Math.floor((S - w) / 2) + i * 4, 5, ink));
}

export const PILE_W = 100;
export const PILE_H = 56;

/** Draw a pyramid pile of `count` coins. */
export function renderPile(canvas, count) {
  canvas.width = PILE_W;
  canvas.height = PILE_H;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, PILE_W, PILE_H);
  const size = 10, dx = 7, dy = 5;
  let left = count;
  let row = 0;
  let perRow = 13;
  while (left > 0 && perRow > 0) {
    const n = Math.min(perRow, left);
    const rowW = (perRow - 1) * dx + size;
    const startX = Math.round((PILE_W - rowW) / 2);
    const y = PILE_H - size - 1 - row * dy;
    // fill a row from the centre outwards so partial rows look natural
    const order = [...Array(perRow).keys()].sort(
      (a, b) => Math.abs(a - (perRow - 1) / 2) - Math.abs(b - (perRow - 1) / 2)
    ).slice(0, n).sort((a, b) => a - b);
    order.forEach((i) => drawCoin(ctx, startX + i * dx, y, size, 1));
    left -= n;
    perRow -= 2;
    row++;
  }
}
