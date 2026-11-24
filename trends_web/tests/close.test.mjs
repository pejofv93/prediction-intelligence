// Cerrar la ficha: node --test trends_web/tests/close.test.mjs
// El fallo del 24-nov-2026 era de CSS: #match { display: flex } ganaba al atributo hidden y la
// ficha seguía a la vista tras "Cerrar". La prueba en navegador (ordenador y móvil 390 px) se hizo
// aparte; aquí se fija lo que no puede volver a faltar.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const css = readFileSync(new URL("../site/style.css", import.meta.url), "utf8");
const js = readFileSync(new URL("../site/app.js", import.meta.url), "utf8");

test("hidden oculta siempre, aunque la sección tenga display propio", () => {
  assert.match(css, /\[hidden\]\s*\{\s*display:\s*none\s*!important;?\s*\}/);
});

test("la ficha se cierra con el botón, Escape, pulsando fuera y con atrás", () => {
  assert.match(js, /text: "Cerrar"[^\n]*onclick: closeMatch/);
  assert.match(js, /e\.key === "Escape"[^\n]*closeMatch\(\)/);
  assert.match(js, /addEventListener\("click"[\s\S]{0,300}closeMatch\(\)/);
  assert.match(js, /history\.pushState\(\{ ficha: true \}/);
  assert.match(js, /addEventListener\("popstate"/);
});
