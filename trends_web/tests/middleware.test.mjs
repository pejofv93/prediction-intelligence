// Tests de la protección de la web: node --test trends_web/tests/
import test from "node:test";
import assert from "node:assert/strict";
import { onRequest, parseBasic } from "../functions/_middleware.js";

const basic = (u, p) => "Basic " + Buffer.from(`${u}:${p}`, "utf8").toString("base64");
const ENV = { TRENDS_WEB_USER: "lector", TRENDS_WEB_PASS: "clave-de-prueba ñ:con:dos-puntos" };

async function call(env, auth, path = "/data/rates.json") {
  let served = false;
  const headers = auth ? { Authorization: auth } : {};
  const res = await onRequest({
    request: new Request(`https://ejemplo.pages.dev${path}`, { headers }),
    env,
    next: async () => { served = true; return new Response('{"datos":1}', { status: 200 }); },
  });
  return { res, served };
}

test("sin credenciales: 401, pide usuario y no sirve nada", async () => {
  for (const path of ["/", "/data/rates.json", "/app.js"]) {
    const { res, served } = await call(ENV, null, path);
    assert.equal(res.status, 401);
    assert.match(res.headers.get("WWW-Authenticate"), /^Basic realm="Tendencias"/);
    assert.equal(served, false);
  }
});

test("usuario o contraseña incorrectos: 401", async () => {
  for (const auth of [basic("lector", "otra"), basic("otro", ENV.TRENDS_WEB_PASS), basic("", ""),
                      "Basic !!!", "Bearer xyz", "Basic " + Buffer.from("sin-dos-puntos").toString("base64")]) {
    const { res, served } = await call(ENV, auth);
    assert.equal(res.status, 401, auth);
    assert.equal(served, false);
  }
});

test("credenciales correctas: sirve y marca la respuesta como privada", async () => {
  const { res, served } = await call(ENV, basic("lector", ENV.TRENDS_WEB_PASS));
  assert.equal(res.status, 200);
  assert.equal(served, true);
  assert.equal(res.headers.get("Cache-Control"), "private, no-store");
  assert.equal(res.headers.get("X-Robots-Tag"), "noindex, nofollow");
  assert.equal(await res.text(), '{"datos":1}');
});

test("falla cerrada: sin contraseña o sin usuario configurados deniega todo, incluso con credenciales", async () => {
  for (const env of [{}, { TRENDS_WEB_USER: "lector" }, { TRENDS_WEB_PASS: "x" },
                     { TRENDS_WEB_USER: "", TRENDS_WEB_PASS: "" }]) {
    for (const auth of [null, basic("lector", "x"), basic("", "")]) {
      const { res, served } = await call(env, auth);
      assert.equal(res.status, 503);
      assert.equal(served, false);
    }
  }
});

test("parseBasic: contraseña con ':' y caracteres no ASCII", () => {
  assert.deepEqual(parseBasic(basic("a", "b:c ñ")), { user: "a", pass: "b:c ñ" });
  assert.equal(parseBasic(null), null);
});
