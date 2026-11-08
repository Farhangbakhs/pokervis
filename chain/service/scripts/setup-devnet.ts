/**
 * One-time devnet setup:
 *  1. server keypair (generated into .keys/server.json if missing; never printed, never committed)
 *  2. check SOL balance (tries one devnet airdrop if low)
 *  3. create the CHIP mint (decimals 0) and persist it
 */
import fs from "node:fs";
import path from "node:path";
import { Keypair, LAMPORTS_PER_SOL } from "@solana/web3.js";
import { Chain, assertDevnet, explorer } from "../src/chain.ts";

const rpc = process.env.SOLANA_RPC ?? "https://api.devnet.solana.com";
assertDevnet(rpc);
const keypairPath = process.env.SERVER_KEYPAIR ?? ".keys/server.json";

if (!fs.existsSync(keypairPath)) {
  fs.mkdirSync(path.dirname(keypairPath), { recursive: true });
  const kp = Keypair.generate();
  fs.writeFileSync(keypairPath, JSON.stringify(Array.from(kp.secretKey)), { mode: 0o600 });
  console.log(`Generated new server keypair at ${keypairPath}`);
}

const chain = new Chain({
  rpc, keypairPath, stateDir: process.env.CHAIN_STATE_DIR ?? ".state", maxBuyin: Number(process.env.MAX_BUYIN ?? 5000),
});
const pub = chain.payer.publicKey;
console.log(`\nServer wallet (fee payer + mint authority): ${pub.toBase58()}`);

let bal = await chain.connection.getBalance(pub);
console.log(`Balance: ${bal / LAMPORTS_PER_SOL} SOL`);
if (bal < 0.5 * LAMPORTS_PER_SOL) {
  try {
    console.log("Low balance, requesting a 1 SOL devnet airdrop (rate-limited, one try)...");
    const sig = await chain.connection.requestAirdrop(pub, LAMPORTS_PER_SOL);
    await chain.connection.confirmTransaction(sig, "confirmed");
    bal = await chain.connection.getBalance(pub);
    console.log(`Balance now: ${bal / LAMPORTS_PER_SOL} SOL`);
  } catch (e) {
    console.log(`Airdrop failed (${(e as Error).message}).`);
  }
}
if (bal < 0.05 * LAMPORTS_PER_SOL) {
  console.log(`\nNeed devnet SOL. Send ~1-2 SOL to ${pub.toBase58()} (e.g. from Phantom on devnet, or https://faucet.solana.com), then re-run: npm run setup`);
  process.exit(2);
}

const mint = await chain.ensureMint();
console.log(`CHIP mint (decimals 0): ${mint.toBase58()}`);
console.log(`Explorer: https://explorer.solana.com/address/${mint.toBase58()}?cluster=devnet`);
fs.mkdirSync("../", { recursive: true });
fs.writeFileSync("../devnet.json", JSON.stringify({ cluster: "devnet", mode: "custodial", server: pub.toBase58(), mint: mint.toBase58() }, null, 2));
void explorer;
console.log("\nSetup complete. Start the service with: npm start");
