// Fichas de Europa en la web: node --test trends_web/tests/uefa.test.mjs
// Grupo "Europa · Champions League" (uno por competición), antes que copas, femenino y baloncesto, y
// un fallo al montar Europa no tumba la lista. Se evalúan los trozos de app.js que lo deciden, con un
// el() de mentira (sin DOM). Equipos inventados.
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
  `${code}; return { statNames, groupTitle, groupKey, isUefa, safeGroups, UEFA_FAIL, BASKET_FAIL };`)(el, num);

const uefa = (over = {}) => ({ group: "uefa", div: "CL", comp_name: "Champions League",
                               home: "Zorrovia Norte", away: "Quimeria Valle", stats: {}, ...over });

test("grupo propio por competición: Europa · Champions League, sin mezclarse con una liga", () => {
  assert.equal(api.groupTitle(uefa()), "Europa · Champions League");
  assert.equal(api.groupKey(uefa()), "uefa:CL");
  assert.equal(api.groupTitle(uefa({ div: "ECL", comp_name: undefined })), "Europa · Conference League");
  assert.equal(api.groupKey(uefa({ div: "EL" })), "uefa:EL");
  assert.equal(api.statNames(uefa()).corners, "Córners");      // estadísticas de fútbol
  assert.equal(api.groupKey({ group: "men", div: "SP1" }), "SP1");
  assert.equal(api.groupKey({ group: "basket", div: "EUR" }), "basket:EUR");
});

test("un grupo de Europa que falla se cambia por su aviso y el fútbol se pinta igual", () => {
  const futbol = { group: "men", div: "SP1", home: "Ciudad A", away: "Ciudad B" };
  const warn = console.warn;
  console.warn = () => {};
  try {
    const out = api.safeGroups([["SP1", [futbol]], ["uefa:CL", [uefa()]]], (key) => {
      if (key.startsWith("uefa")) throw new TypeError("ficha mal formada");
      return el("div", { text: key });
    });
    assert.equal(out[0].text, "SP1");
    assert.equal(out[1].text, api.UEFA_FAIL);
  } finally {
    console.warn = warn;
  }
});
