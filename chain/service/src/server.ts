import crypto from "node:crypto";
import Fastify from "fastify";
import { Chain, type Config } from "./chain.ts";

const apiKey = process.env.CHAIN_API_KEY ?? "";
if (apiKey.length < 24) {
  console.error("CHAIN_API_KEY missing/too short. Run: python tools/init_env.py");
  process.exit(1);
}

const cfg: Config = {
  rpc: process.env.SOLANA_RPC ?? "https://api.devnet.solana.com",
  keypairPath: process.env.SERVER_KEYPAIR ?? ".keys/server.json",
  stateDir: process.env.CHAIN_STATE_DIR ?? ".state",
  maxBuyin: Number(process.env.MAX_BUYIN ?? 5000),
  lamportsPerChip: Number(process.env.LAMPORTS_PER_CHIP ?? 10_000),
};
const chain = new Chain(cfg);

const app = Fastify({ logger: { level: "info", redact: ["req.headers.x-api-key"] }, bodyLimit: 64 * 1024 });

function keyOk(got: unknown): boolean {
  if (typeof got !== "string") return false;
  const a = crypto.createHash("sha256").update(got).digest();
  const b = crypto.createHash("sha256").update(apiKey).digest();
  return crypto.timingSafeEqual(a, b);
}

// Everything except /health needs the shared secret (only the game server knows it).
app.addHook("onRequest", async (req, reply) => {
  if (req.url === "/health") return;
  if (!keyOk(req.headers["x-api-key"])) return reply.code(401).send({ error: "unauthorized" });
});

app.setErrorHandler((err: Error, _req, reply) => {
  reply.code(400).send({ error: err.message });
});

type Body = Record<string, unknown>;
app.get("/health", async () => ({ ok: true, mode: "custodial", cluster: "devnet", server: chain.payer.publicKey.toBase58() }));
app.get("/balances", async () => ({ mint: chain.mint.toBase58(), balances: await chain.balances() }));
app.get("/info", async () => chain.info());
app.post("/buyin_tx", async (req) => { const b = req.body as Body; return chain.buyinTx(b.pubkey, b.chips); });
app.post("/verify_buyin", async (req) => { const b = req.body as Body; return chain.verifyBuyin(b.signature, b.pubkey); });
app.post("/cashout_sol", async (req) => { const b = req.body as Body; return chain.cashoutSol(b.pubkey, b.amount); });
app.get("/balance/:pubkey", async (req) => ({ balance: await chain.balanceOfWallet((req.params as { pubkey: string }).pubkey) }));
app.post("/fund", async (req) => { const b = req.body as Body; return chain.fund(b.pubkey, b.lamports); });
app.post("/begin_hand", async (req) => chain.beginHand((req.body as Body).hand_id));
app.post("/abort_hand", async (req) => chain.abortHand((req.body as Body).hand_id));
app.post("/settle", async (req) => {
  const b = req.body as Body;
  return chain.settle(b.hand_id, b.deltas, b.showdown_hash);
});

const port = Number(process.env.CHAIN_PORT ?? 8787);
await app.listen({ host: "127.0.0.1", port });     // loopback only: never exposed to the LAN
