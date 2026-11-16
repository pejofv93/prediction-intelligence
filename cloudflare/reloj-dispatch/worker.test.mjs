// node --test cloudflare/reloj-dispatch/worker.test.mjs  (sin red: fetch simulado)
import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { tick } from "./worker.js";

test("llama a /dispatch con la cabecera y el User-Agent del reloj", async () => {
  const calls = [];
  const fake = async (url, opts) => { calls.push({ url, opts }); return { status: 200 }; };
  const st = await tick({ DISPATCH_URL: "https://bot.example/", CLOUD_RUN_TOKEN: "t" }, fake);
  assert.equal(st, 200);
  assert.equal(calls[0].url, "https://bot.example/dispatch");
  assert.equal(calls[0].opts.method, "POST");
  assert.equal(calls[0].opts.headers["x-cloud-token"], "t");
  assert.equal(calls[0].opts.headers["user-agent"], "reloj-cloudflare/1");
});

test("sin secretos no llama", async () => {
  let n = 0;
  assert.equal(await tick({}, async () => { n++; }), 0);
  assert.equal(n, 0);
});

test("cualquier visita recibe 404", async () => {
  assert.equal((await worker.fetch()).status, 404);
});
