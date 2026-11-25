// Fichas de baloncesto en la web: node --test trends_web/tests/basket.test.mjs
// Grupo "Baloncesto · Euroliga", sus estadísticas (puntos, rebotes, asistencias, triples) y píldoras
// con las MISMAS reglas que las ligas de fútbol. Se evalúan los trozos de app.js que lo deciden, con
// un el() de mentira (sin DOM). Equipos inventados.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const src = readFileSync(new URL("../site/app.js", import.meta.url), "utf8");
const pick = (re) => { const m = src.match(re); assert.ok(m, `no está en app.js: ${re}`); return m[0]; };
const code = [
  pick(/const DIV_NAMES = \{[\s\S]*?\};/),
  pick(/const STAT_NAMES = [\s\S]*?const statNames = [^\n]*\n/),
  pick(/const isWomen[\s\S]*?const groupKey = [^\n]*\n/),
  pick(/const PILL_MIN[\s\S]*?(?=\nfunction sideLine)/),
].join("\n");
const el = (tag, props = {}, ...kids) => ({ tag, text: props.text, kids });
const num = (x) => String(x).replace(".", ",");
const api = new Function("el", "num",
  `${code}; return { statNames, groupTitle, groupKey, matchPills, isBasket };`)(el, num);

const blk = (label, lines) => ({ label, highlight: label === "ventaja medida", lines });
function basketMatch(over = {}) {
  const stats = {};
  for (const s of ["pts", "reb", "ast", "fg3m"]) {
    stats[s] = { available: true, blocks: {
      home: blk("solo referencia", [[70.5, 0.8], [80.5, 0.5]]),
      away: blk("solo referencia", [[70.5, 0.8], [80.5, 0.5]]),
      total: blk("ventaja medida", [[150.5, 0.8], [160.5, 0.7]]) } };
  }
  return { group: "basket", div: "EUR", league_name: "Euroliga", home: "Zorrovia Norte",
           away: "Quimeria Valle", low_sample: false, stats, ...over };
}

test("grupo propio: Baloncesto · Euroliga, sin mezclarse con una liga de fútbol", () => {
  const m = basketMatch();
  assert.equal(api.groupTitle(m), "Baloncesto · Euroliga");
  assert.equal(api.groupKey(m), "basket:EUR");
  assert.equal(api.groupTitle({ ...m, league_name: undefined }), "Baloncesto · Euroliga");
  assert.deepEqual(Object.keys(api.statNames(m)), ["pts", "reb", "ast", "fg3m"]);
  assert.equal(api.statNames({ group: "men" }).corners, "Córners");
  assert.equal(api.groupKey({ group: "men", div: "SP1" }), "SP1");
});

test("píldoras: solo mercados por equipo con ventaja medida, línea más alta entre 60 y 90 %", () => {
  const m = basketMatch();
  assert.deepEqual(api.matchPills(m), []);              // el total no da píldora, aunque tenga ventaja
  m.stats.pts.blocks.home = blk("ventaja medida", [[75.5, 0.86], [80.5, 0.71], [85.5, 0.55], [90.5, 0.3]]);
  m.stats.fg3m.blocks.away = blk("ventaja medida", [[7.5, 0.95], [9.5, 0.62]]);
  const out = api.matchPills(m);
  assert.equal(out.length, 2);
  assert.deepEqual(out.map((x) => [x.side, x.p]), [["home", 0.71], ["away", 0.62]]);   // de mayor a menor %
  const text = (b) => b.map((k) => (typeof k === "string" ? k : k.text)).join("");
  assert.equal(text(out[0].body), "Zorrovia Norte más de 80,5 puntos");
  assert.equal(text(out[1].body), "Quimeria Valle más de 9,5 triples");
  assert.deepEqual(api.matchPills({ ...m, low_sample: true }), []);
});

test("como mucho 5 píldoras", () => {
  const m = basketMatch();
  for (const s of ["pts", "reb", "ast"]) {
    m.stats[s].blocks.home = blk("ventaja medida", [[10.5, 0.7]]);
    m.stats[s].blocks.away = blk("ventaja medida", [[10.5, 0.65]]);
  }
  assert.equal(api.matchPills(m).length, 5);
});
