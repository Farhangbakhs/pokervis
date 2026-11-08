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
app.post("/join", async (req) => { const b = req.body as Body; return chain.join(b.pubkey, b.amount); });
app.post("/cashout", async (req) => { const b = req.body as Body; return chain.cashout(b.pubkey, b.amount); });
app.post("/begin_hand", async (req) => chain.beginHand((req.body as Body).hand_id));
app.post("/abort_hand", async (req) => chain.abortHand((req.body as Body).hand_id));
app.post("/settle", async (req) => {
  const b = req.body as Body;
  return chain.settle(b.hand_id, b.deltas, b.showdown_hash);
});

const port = Number(process.env.CHAIN_PORT ?? 8787);
await app.listen({ host: "127.0.0.1", port });     // loopback only: never exposed to the LAN
