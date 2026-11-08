/**
 * One-off devnet refund: the house sends back (its whole balance - a fee reserve) to one address.
 *   npx tsx --env-file-if-exists=../../.env scripts/refund.ts <destination pubkey> [reserveSOL=0.05]
 * Devnet only. Signs with the server keypair (.keys/server.json). Prints the Explorer link.
 */
import fs from "node:fs";
import { Connection, Keypair, LAMPORTS_PER_SOL, PublicKey, SystemProgram, Transaction, sendAndConfirmTransaction } from "@solana/web3.js";
import { assertDevnet, explorer } from "../src/chain.ts";

const rpc = process.env.SOLANA_RPC ?? "https://api.devnet.solana.com";
assertDevnet(rpc);
const dest = new PublicKey(process.argv[2] ?? "");
const reserve = Math.round(Number(process.argv[3] ?? "0.05") * LAMPORTS_PER_SOL);

const conn = new Connection(rpc, "confirmed");
const house = Keypair.fromSecretKey(Uint8Array.from(JSON.parse(fs.readFileSync(process.env.SERVER_KEYPAIR ?? ".keys/server.json", "utf8"))));
const bal = await conn.getBalance(house.publicKey);
const amount = bal - reserve;
console.log(`house ${house.publicKey.toBase58()} has ${bal / LAMPORTS_PER_SOL} SOL; keeping ${reserve / LAMPORTS_PER_SOL}`);
if (amount <= 0) throw new Error("nothing to refund after the reserve");
const destBefore = await conn.getBalance(dest);
const sig = await sendAndConfirmTransaction(conn, new Transaction().add(SystemProgram.transfer({ fromPubkey: house.publicKey, toPubkey: dest, lamports: amount })), [house]);
console.log(`sent ${amount / LAMPORTS_PER_SOL} SOL to ${dest.toBase58()}`);
console.log(`destination: ${destBefore / LAMPORTS_PER_SOL} -> ${(await conn.getBalance(dest)) / LAMPORTS_PER_SOL} SOL`);
console.log(explorer(sig));
