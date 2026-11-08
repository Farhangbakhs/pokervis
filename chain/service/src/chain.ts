import fs from "node:fs";
import path from "node:path";
import {
  Connection, Keypair, PublicKey, Transaction, TransactionInstruction, sendAndConfirmTransaction,
} from "@solana/web3.js";
import {
  createAccount, createMint, createTransferInstruction, getAccount, getOrCreateAssociatedTokenAccount,
  mintTo, transfer,
} from "@solana/spl-token";
import { netTransfers, type Transfer } from "./netting.ts";

export const MEMO_PROGRAM = new PublicKey("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr");

export interface Config {
  rpc: string;
  keypairPath: string;
  stateDir: string;
  maxBuyin: number;
}

interface Settled { signature: string; explorer_url: string; transfers: Transfer[]; at: string }
interface State {
  mint: string | null;
  seats: Record<string, string>;          // player wallet pubkey -> CHIP token account (owned by the server)
  settled: Record<string, Settled>;       // hand_id -> result (idempotency / replay protection)
  begun: string | null;                   // hand currently open for settlement
}

/** Devnet / localhost only. This project must never point at mainnet. */
export function assertDevnet(rpc: string): void {
  const u = new URL(rpc);
  const ok = u.hostname.includes("devnet") || u.hostname === "localhost" || u.hostname === "127.0.0.1";
  if (!ok) throw new Error(`Refusing non-devnet RPC: ${u.hostname}`);
}

export function loadKeypair(p: string): Keypair {
  return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(fs.readFileSync(p, "utf8"))));
}

export function parsePubkey(s: unknown): PublicKey {
  if (typeof s !== "string" || s.length < 32 || s.length > 44) throw new Error("invalid pubkey");
  return new PublicKey(s);                 // throws on bad base58
}

export const explorer = (sig: string) => `https://explorer.solana.com/tx/${sig}?cluster=devnet`;

export class Chain {
  readonly connection: Connection;
  readonly payer: Keypair;
  private state: State;
  private statePath: string;
  private lock: Promise<unknown> = Promise.resolve();

  constructor(private cfg: Config) {
    assertDevnet(cfg.rpc);
    this.connection = new Connection(cfg.rpc, "confirmed");
    this.payer = loadKeypair(cfg.keypairPath);
    fs.mkdirSync(cfg.stateDir, { recursive: true });
    this.statePath = path.join(cfg.stateDir, "state.json");
    this.state = fs.existsSync(this.statePath)
      ? (JSON.parse(fs.readFileSync(this.statePath, "utf8")) as State)
      : { mint: null, seats: {}, settled: {}, begun: null };
  }

  /** Serialise all mutating operations (one settlement at a time). */
  private serial<T>(fn: () => Promise<T>): Promise<T> {
    const run = this.lock.then(fn, fn);
    this.lock = run.catch(() => undefined);
    return run;
  }

  private save(): void {
    const tmp = this.statePath + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(this.state, null, 1));
    fs.renameSync(tmp, this.statePath);
  }

  get mint(): PublicKey {
    if (!this.state.mint) throw new Error("CHIP mint not created; run `npm run setup`");
    return new PublicKey(this.state.mint);
  }

  async ensureMint(): Promise<PublicKey> {
    if (!this.state.mint) {
      // decimals 0: 1 CHIP = 1 base unit, so chips are whole numbers and pot maths is exact.
      const m = await createMint(this.connection, this.payer, this.payer.publicKey, null, 0);
      this.state.mint = m.toBase58();
      this.save();
    }
    return this.mint;
  }

  private async seatAccount(pubkey: PublicKey): Promise<PublicKey> {
    const key = pubkey.toBase58();
    let addr = this.state.seats[key];
    if (!addr) {
      const acct = await createAccount(this.connection, this.payer, this.mint, this.payer.publicKey, Keypair.generate());
      addr = acct.toBase58();
      this.state.seats[key] = addr;
      this.save();
    }
    return new PublicKey(addr);
  }

  private async balanceOf(acct: PublicKey): Promise<number> {
    return Number((await getAccount(this.connection, acct)).amount);
  }

  async balances(): Promise<Record<string, number>> {
    const out: Record<string, number> = {};
    for (const [pk, acct] of Object.entries(this.state.seats)) out[pk] = await this.balanceOf(new PublicKey(acct));
    return out;
  }

  /** Play-money buy-in: mints `amount` CHIP into the player's server-held seat account. No player signature needed. */
  join(pubkeyStr: unknown, amount: unknown) {
    return this.serial(async () => {
      const pk = parsePubkey(pubkeyStr);
      if (!Number.isSafeInteger(amount) || (amount as number) < 1 || (amount as number) > this.cfg.maxBuyin)
        throw new Error(`amount must be an integer 1..${this.cfg.maxBuyin}`);
      const acct = await this.seatAccount(pk);
      const bal = await this.balanceOf(acct);
      if (bal + (amount as number) > this.cfg.maxBuyin) throw new Error(`seat balance would exceed ${this.cfg.maxBuyin}`);
      const signature = await mintTo(this.connection, this.payer, this.mint, acct, this.payer, amount as number);
      return { seat_account: acct.toBase58(), balance: bal + (amount as number), signature, explorer_url: explorer(signature) };
    });
  }

  /** Move chips from the seat account to the player's own wallet (their ATA). Server pays all fees. */
  cashout(pubkeyStr: unknown, amount?: unknown) {
    return this.serial(async () => {
      const pk = parsePubkey(pubkeyStr);
      if (this.state.begun) throw new Error("hand in progress");
      const acct = await this.seatAccount(pk);
      const bal = await this.balanceOf(acct);
      const amt = amount === undefined || amount === null ? bal : amount;
      if (!Number.isSafeInteger(amt) || (amt as number) < 1 || (amt as number) > bal) throw new Error("bad cash-out amount");
      const dest = await getOrCreateAssociatedTokenAccount(this.connection, this.payer, this.mint, pk);
      const signature = await transfer(this.connection, this.payer, acct, dest.address, this.payer, amt as number);
      return { amount: amt, balance: bal - (amt as number), signature, explorer_url: explorer(signature) };
    });
  }

  beginHand(handId: unknown) {
    return this.serial(async () => {
      if (typeof handId !== "string" || !handId || handId.length > 64) throw new Error("bad hand_id");
      if (this.state.settled[handId]) throw new Error("hand_id already settled");
      if (this.state.begun && this.state.begun !== handId) throw new Error(`hand ${this.state.begun} still open`);
      this.state.begun = handId;
      this.save();
      return { ok: true, hand_id: handId };
    });
  }

  abortHand(handId: unknown) {
    return this.serial(async () => {
      if (this.state.begun !== handId) throw new Error("that hand is not open");
      this.state.begun = null;
      this.save();
      return { ok: true };
    });
  }

  /**
   * Atomic settlement: ONE transaction containing every loser->winner transfer plus a memo
   * with hand_id and showdown hash. Idempotent per hand_id. Zero-sum enforced before sending.
   */
  settle(handId: unknown, deltas: unknown, showdownHash: unknown) {
    return this.serial(async () => {
      if (typeof handId !== "string") throw new Error("bad hand_id");
      const done = this.state.settled[handId];
      if (done) return { ...done, replay: true };
      if (this.state.begun !== handId) throw new Error("hand not begun");
      if (typeof showdownHash !== "string" || !/^[0-9a-f]{64}$/.test(showdownHash)) throw new Error("bad showdown_hash");
      if (typeof deltas !== "object" || deltas === null) throw new Error("bad deltas");

      const d = deltas as Record<string, number>;
      const transfers = netTransfers(d);                      // throws unless integers and sum == 0
      const accts: Record<string, PublicKey> = {};
      for (const k of Object.keys(d)) {
        if (!this.state.seats[k]) throw new Error(`unknown seat ${k}`);
        accts[k] = new PublicKey(this.state.seats[k]!);
      }
      for (const [k, v] of Object.entries(d)) {               // no balance may go negative
        if (v < 0 && (await this.balanceOf(accts[k]!)) < -v) throw new Error(`insufficient balance for ${k}`);
      }

      const tx = new Transaction();
      for (const t of transfers)
        tx.add(createTransferInstruction(accts[t.from]!, accts[t.to]!, this.payer.publicKey, t.amount));
      tx.add(new TransactionInstruction({
        keys: [], programId: MEMO_PROGRAM,
        data: Buffer.from(`autopoker:${handId}:${showdownHash.slice(0, 32)}`, "utf8"),
      }));
      const signature = tx.instructions.length > 1
        ? await sendAndConfirmTransaction(this.connection, tx, [this.payer])
        : await sendAndConfirmTransaction(this.connection, tx, [this.payer]);   // chop: memo-only receipt

      const rec: Settled = { signature, explorer_url: explorer(signature), transfers, at: new Date().toISOString() };
      this.state.settled[handId] = rec;
      this.state.begun = null;
      this.save();
      return rec;
    });
  }
}
