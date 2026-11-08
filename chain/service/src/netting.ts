export type Transfer = { from: string; to: string; amount: number };

/**
 * Turn zero-sum deltas ({player: chips won(+)/lost(-)}) into the minimal list of
 * loser -> winner transfers. Throws on non-integers, non-zero sum, or absurd sizes.
 */
export function netTransfers(deltas: Record<string, number>): Transfer[] {
  const entries = Object.entries(deltas);
  if (entries.length < 2) throw new Error("need at least 2 players");
  let sum = 0;
  for (const [k, v] of entries) {
    if (!Number.isSafeInteger(v)) throw new Error(`delta for ${k} must be an integer`);
    sum += v;
  }
  if (sum !== 0) throw new Error(`deltas must sum to 0 (got ${sum})`);

  const losers = entries.filter(([, v]) => v < 0).map(([k, v]) => ({ k, left: -v })).sort((a, b) => b.left - a.left);
  const winners = entries.filter(([, v]) => v > 0).map(([k, v]) => ({ k, left: v })).sort((a, b) => b.left - a.left);

  const out: Transfer[] = [];
  let i = 0;
  let j = 0;
  while (i < losers.length && j < winners.length) {
    const l = losers[i]!;
    const w = winners[j]!;
    const amt = Math.min(l.left, w.left);
    out.push({ from: l.k, to: w.k, amount: amt });
    l.left -= amt;
    w.left -= amt;
    if (l.left === 0) i++;
    if (w.left === 0) j++;
  }
  return out;
}
