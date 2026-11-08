/**
 * Real-devnet check of the SOL layer against a RUNNING chain service (what a Phantom user's buy-in/cash-out does):
 *   throwaway wallet <- 0.01 SOL from the house, builds + signs the buy-in tx, chain verifies it and mints CHIP,
 *   then cashes out: chips return, SOL comes back. Run: npx tsx --env-file-if-exists=../../.env scripts/e2e-sol.ts
 */
import fs from "node:fs";
import { Connection, Keypair, LAMPORTS_PER_SOL, SystemProgram, Transaction, sendAndConfirmTransaction } from "@solana/web3.js";

const rpc = process.env.SOLANA_RPC ?? "https://api.devnet.solana.com";
if (!new URL(rpc).hostname.includes("devnet")) throw new Error("devnet only");
const url = `http://127.0.0.1:${process.env.CHAIN_PORT ?? 8787}`;
const headers = { "content-type": "application/json", "x-api-key": process.env.CHAIN_API_KEY ?? "" };
const call = async (method: string, path: string, body?: unknown) => {
  const r = await fetch(url + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const j = await r.json();
  if (!r.ok) throw new Error(`${path}: ${j.error}`);
  return j;
};

const conn = new Connection(rpc, "confirmed");
const house = Keypair.fromSecretKey(Uint8Array.from(JSON.parse(fs.readFileSync(".keys/server.json", "utf8"))));
const player = Keypair.generate();
const pk = player.publicKey.toBase58();
const info = await call("GET", "/info");
console.log("house", info.house, "| 1 CHIP =", info.lamports_per_chip, "lamports | player", pk);

await sendAndConfirmTransaction(conn, new Transaction().add(SystemProgram.transfer({ fromPubkey: house.publicKey, toPubkey: player.publicKey, lamports: 0.01 * LAMPORTS_PER_SOL })), [house]);
const before = await conn.getBalance(player.publicKey);
console.log("player funded:", before / LAMPORTS_PER_SOL, "SOL");

const { tx } = await call("POST", "/buyin_tx", { pubkey: pk, chips: 500 });
const t = Transaction.from(Buffer.from(tx, "base64"));
t.partialSign(player);                                                    // what Phantom does when the user approves
const sig = await conn.sendRawTransaction(t.serialize());
console.log("buy-in tx sent:", sig);
const v = await call("POST", "/verify_buyin", { signature: sig, pubkey: pk });
console.log("credited:", v.chips, "CHIP, bankroll", v.balance);
if (v.chips !== 500 || v.balance !== 500) throw new Error("wrong credit");
const again = await call("POST", "/verify_buyin", { signature: sig, pubkey: pk });
if (again.balance !== 500) throw new Error("replay double-credited");
console.log("replay is idempotent: ok");

const out = await call("POST", "/cashout_sol", { pubkey: pk });
const after = await conn.getBalance(player.publicKey);
console.log("cashed out", out.amount, "CHIP ->", out.sol, "SOL, tx", out.explorer_url);
console.log("player SOL: before buy-in", before / LAMPORTS_PER_SOL, "-> after cash-out", after / LAMPORTS_PER_SOL, "(difference is the network fee)");
if (Math.abs(after - (before - 5000)) > 10_000) throw new Error("SOL did not come back");
console.log("SOL LAYER OK");
