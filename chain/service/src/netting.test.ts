import { test } from "node:test";
import assert from "node:assert/strict";
import { netTransfers } from "./netting.ts";

const total = (t: { amount: number }[]) => t.reduce((a, x) => a + x.amount, 0);

test("two players", () => {
  assert.deepEqual(netTransfers({ a: 50, b: -50 }), [{ from: "b", to: "a", amount: 50 }]);
});

test("three-way: one winner, two losers", () => {
  const t = netTransfers({ a: 300, b: -100, c: -200 });
  assert.equal(total(t), 300);
  assert.ok(t.every((x) => x.to === "a"));
});

test("two winners split across losers, conserves chips", () => {
  const d = { a: 200, b: 100, c: -300 };
  const t = netTransfers(d);
  assert.equal(total(t), 300);
  const net: Record<string, number> = {};
  for (const x of t) {
    net[x.from] = (net[x.from] ?? 0) - x.amount;
    net[x.to] = (net[x.to] ?? 0) + x.amount;
  }
  assert.deepEqual(net, d);
});

test("all zero (chop) needs no transfers", () => {
  assert.deepEqual(netTransfers({ a: 0, b: 0 }), []);
});

test("rejects non-zero-sum, fractions, single player", () => {
  assert.throws(() => netTransfers({ a: 10, b: -5 }));
  assert.throws(() => netTransfers({ a: 1.5, b: -1.5 }));
  assert.throws(() => netTransfers({ a: 0 }));
});
