import fs from "node:fs";
import path from "node:path";
import {
  Connection, Keypair, PublicKey, SystemProgram, Transaction, TransactionInstruction, sendAndConfirmTransaction,
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
  lamportsPerChip: number;                 // 1 CHIP = this many lamports (default 10_000: 1000 CHIP = 0.01 SOL)
}

interface Settled { signature: string; explorer_url: string; transfers: Transfer[]; at: string }
interface State {
  mint: string | null;
  seats: Record<string, string>;          // player wallet pubkey -> CHIP token account (owned by the server)
  settled: Record<string, Settled>;       // hand_id -> result (idempotency / replay protection)
  begun: string | null;                   // hand currently open for settlement
  bank: string | null;                    // CHIP token account that receives chips cashed out for SOL
  deposits: Record<string, { chips: number; mint_sig: string }>;   // SOL buy-in signature -> credited (idempotency)
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
    const fresh: State = { mint: null, seats: {}, settled: {}, begun: null, bank: null, deposits: {} };
    this.state = fs.existsSync(this.statePath)
      ? { ...fresh, ...(JSON.parse(fs.readFileSync(this.statePath, "utf8")) as Partial<State>) }
      : fresh;
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

  /** All seat balances in ONE RPC round trip (getMultipleAccounts) instead of one call per player. */
  async balances(): Promise<Record<string, number>> {
    const entries = Object.entries(this.state.seats);
    const out: Record<string, number> = {};
    for (let i = 0; i < entries.length; i += 100) {
      const chunk = entries.slice(i, i + 100);
      const infos = await this.connection.getMultipleAccountsInfo(chunk.map(([, a]) => new PublicKey(a)));
      chunk.forEach(([pk], j) => {
        const data = infos[j]?.data;
        out[pk] = data && data.length >= 72 ? Number(Buffer.from(data).readBigUInt64LE(64)) : 0;   // SPL token account: amount at byte 64
      });
    }
    return out;
  }

  /** One wallet's bankroll (0 if it has never played). */
  async balanceOfWallet(pubkeyStr: unknown): Promise<number> {
    const addr = this.state.seats[parsePubkey(pubkeyStr).toBase58()];
    return addr ? this.balanceOf(new PublicKey(addr)) : 0;
  }

  /**
   * Dev helper for BOT wallets only: the house sends a little devnet SOL so the bot can then BUY IN like any player
   * (its own signed transaction). Capped per call and refused if the wallet already has SOL, so it cannot be farmed.
   */
  fund(pubkeyStr: unknown, lamportsIn: unknown) {
    return this.serial(async () => {
      const pk = parsePubkey(pubkeyStr);
      const lamports = Number(lamportsIn);
      if (!Number.isSafeInteger(lamports) || lamports < 1 || lamports > 100_000_000) throw new Error("fund 1..100000000 lamports");
      if ((await this.connection.getBalance(pk)) >= 50_000_000) throw new Error("that wallet already has devnet SOL");
      const signature = await sendAndConfirmTransaction(this.connection, new Transaction().add(
        SystemProgram.transfer({ fromPubkey: this.payer.publicKey, toPubkey: pk, lamports })), [this.payer]);
      return { lamports, signature, explorer_url: explorer(signature) };
    });
  }

  info() {
    return { house: this.payer.publicKey.toBase58(), mint: this.mint.toBase58(), lamports_per_chip: this.cfg.lamportsPerChip, max_buyin: this.cfg.maxBuyin, cluster: "devnet" };
  }

  /** Unsigned SOL buy-in: player -> house transfer (player is fee payer and signs it in their wallet). */
  async buyinTx(pubkeyStr: unknown, chips: unknown) {
    const pk = parsePubkey(pubkeyStr);
    if (!Number.isSafeInteger(chips) || (chips as number) < 1 || (chips as number) > this.cfg.maxBuyin)
      throw new Error(`chips must be an integer 1..${this.cfg.maxBuyin}`);
    const lamports = (chips as number) * this.cfg.lamportsPerChip;
    const { blockhash, lastValidBlockHeight } = await this.connection.getLatestBlockhash("confirmed");
    const tx = new Transaction({ feePayer: pk, blockhash, lastValidBlockHeight });
    tx.add(SystemProgram.transfer({ fromPubkey: pk, toPubkey: this.payer.publicKey, lamports }));
    tx.add(new TransactionInstruction({ keys: [], programId: MEMO_PROGRAM, data: Buffer.from(`autopoker:buyin:${chips}`, "utf8") }));
    return { tx: tx.serialize({ requireAllSignatures: false }).toString("base64"), lamports, chips, house: this.payer.publicKey.toBase58() };
  }

  /**
   * The player paid the house in SOL (their own wallet signed it). Check it ON CHAIN, then mint the same value in CHIP
   * into their bankroll. The credit comes from the lamports actually transferred, never from what the client claims.
   * Idempotent per signature.
   */
  verifyBuyin(signature: unknown, pubkeyStr: unknown) {
    return this.serial(async () => {
      if (typeof signature !== "string" || signature.length < 80 || signature.length > 100) throw new Error("bad signature");
      const pk = parsePubkey(pubkeyStr);
      const acct = await this.seatAccount(pk);
      const prior = this.state.deposits[signature];
      if (prior) return { chips: prior.chips, balance: await this.balanceOf(acct), signature: prior.mint_sig, deposit_signature: signature, explorer_url: explorer(signature), replay: true };

      let parsed = null;
      for (let i = 0; i < 40 && !parsed; i++) {                         // wait up to ~40 s for the wallet's tx to confirm
        parsed = await this.connection.getParsedTransaction(signature, { commitment: "confirmed", maxSupportedTransactionVersion: 0 });
        if (!parsed) await new Promise((r) => setTimeout(r, 1000));
      }
      if (!parsed) throw new Error("transaction not confirmed yet, try again in a moment");
      if (parsed.meta?.err) throw new Error("that transaction failed on chain");
      const house = this.payer.publicKey.toBase58();
      let lamports = 0;
      for (const ix of parsed.transaction.message.instructions) {
        const p = ix as { program?: string; parsed?: { type?: string; info?: { source?: string; destination?: string; lamports?: number } } };
        if (p.program === "system" && p.parsed?.type === "transfer" && p.parsed.info?.source === pk.toBase58() && p.parsed.info.destination === house)
          lamports += Number(p.parsed.info.lamports ?? 0);
      }
      const chips = Math.floor(lamports / this.cfg.lamportsPerChip);                // exactly what was paid, nothing more
      if (chips < 1) throw new Error("no SOL payment to the table was found in that transaction");
      const mintSig = await mintTo(this.connection, this.payer, this.mint, acct, this.payer, chips);
      this.state.deposits[signature] = { chips, mint_sig: mintSig };
      this.save();
      return { chips, balance: await this.balanceOf(acct), signature: mintSig, deposit_signature: signature, explorer_url: explorer(signature) };
    });
  }

  /** Cash out: ONE atomic transaction returns the chips to the bank and sends the player the SOL for them. */
  cashoutSol(pubkeyStr: unknown, amount?: unknown) {
    return this.serial(async () => {
      const pk = parsePubkey(pubkeyStr);
      if (this.state.begun) throw new Error("hand in progress");
      const acct = await this.seatAccount(pk);
      const bal = await this.balanceOf(acct);
      const amt = amount === undefined || amount === null ? bal : amount;
      if (!Number.isSafeInteger(amt) || (amt as number) < 1 || (amt as number) > bal) throw new Error("nothing to cash out");
      const lamports = (amt as number) * this.cfg.lamportsPerChip;
      const houseBal = await this.connection.getBalance(this.payer.publicKey);
      if (houseBal < lamports + 20_000_000) throw new Error("the house is low on devnet SOL; ask the dealer to top it up");
      if (!this.state.bank) {
        const b = await createAccount(this.connection, this.payer, this.mint, this.payer.publicKey, Keypair.generate());
        this.state.bank = b.toBase58();
        this.save();
      }
      const tx = new Transaction();
      tx.add(createTransferInstruction(acct, new PublicKey(this.state.bank), this.payer.publicKey, amt as number));
      tx.add(SystemProgram.transfer({ fromPubkey: this.payer.publicKey, toPubkey: pk, lamports }));
      tx.add(new TransactionInstruction({ keys: [], programId: MEMO_PROGRAM, data: Buffer.from(`autopoker:cashout:${amt}`, "utf8") }));
      const signature = await sendAndConfirmTransaction(this.connection, tx, [this.payer]);
      return { amount: amt, balance: bal - (amt as number), lamports, sol: lamports / 1e9, signature, explorer_url: explorer(signature) };
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
