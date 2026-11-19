// Iniciales del escudo: node --test trends_web/tests/initials.test.mjs
// initials() de app.js (respaldo para un previews.json sin "badges") debe dar lo mismo que
// badge_initials() de shared/team_display_names.py; los casos son los de
// services/sports-agent/tests/test_team_display_names.py (INITIALS_CASES).
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const src = readFileSync(new URL("../site/app.js", import.meta.url), "utf8");
const body = src.match(/function initials\(name\) \{[\s\S]*?\n\}/);
const initials = new Function(`${body[0]}; return initials;`)();

const CASES = [
  ["Athletic", "ATH"], ["Espanyol", "ESP"], ["Colonia", "COL"], ["Real Madrid", "RM"],
  ["Sporting de Portugal", "SP"], ["Nottingham Forest", "NF"], ["St. Pauli", "SP"],
  ["Atlético", "ATL"], ["Mönchengladbach", "MÖN"], ["PSG", "PSG"], ["ADO Den Haag", "ADH"],
  ["Académico de Viseu", "AV"], ["Willem II", "WI"], ["Saint-Étienne", "SÉ"], ["", "?"],
];

test("initials() de la web = badge_initials() de Python", () => {
  for (const [name, want] of CASES) assert.equal(initials(name), want, name);
});
