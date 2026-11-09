"use strict";
// Web privada de Tendencias: porcentajes por equipo y mercado (data/rates.json, generado por
// trends_web/export_rates.py). Sin dependencias. Todo el texto entra por textContent.

const SPLITS = [["all", "Todos"], ["home", "En casa"], ["away", "Fuera"]];
const LABELS = { encima: "por encima", debajo: "por debajo", en_linea: "en línea con la liga",
                 insuficiente: "muestra insuficiente", sin_liga: "sin media de liga" };
const DIV_NAMES = { SP1: "LaLiga", SP2: "Segunda", I1: "Serie A", E0: "Premier", F1: "Ligue 1",
                    P1: "Primeira", D1: "Bundesliga", N1: "Eredivisie", E1: "Championship",
                    I2: "Serie B", F2: "Ligue 2", D2: "2. Bundesliga" };

const state = { div: null, split: 0, market: null, sort: -1, desc: false, byGap: false };
let DATA = null;

const $ = (id) => document.getElementById(id);
function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const k of kids) if (k != null) n.append(k);
  return n;
}
const pct = (p) => (p == null ? "—" : `${Math.round(p * 100)} %`);
const pts = (p, l) => (p == null || l == null ? null : Math.round((p - l) * 100));
const signed = (x) => (x > 0 ? `+${x}` : `${x}`);
const num = (x) => (x == null ? "—" : Number(x).toLocaleString("es-ES", { maximumFractionDigits: 2 }));

function marketMeta(key) { return DATA.markets.find((m) => m.key === key); }

function readHash() {
  const [div, split, market] = decodeURIComponent(location.hash.slice(1)).split("/");
  if (div && DATA.leagues[div]) state.div = div;
  const s = SPLITS.findIndex(([k]) => k === split);
  if (s >= 0) state.split = s;
  if (market && marketMeta(market)) state.market = market;
}
function writeHash() {
  history.replaceState(null, "", `#${state.div}/${SPLITS[state.split][0]}/${state.market}`);
}

function pills(container, items, isOn, onPick) {
  container.replaceChildren(...items.map(([value, label]) =>
    el("button", { class: "pill", role: "tab", "aria-selected": String(isOn(value)), text: label,
                   onclick: () => onPick(value) })));
}

function renderControls() {
  pills($("leagues"), Object.entries(DATA.leagues).map(([d, l]) => [d, l.name]),
        (d) => d === state.div, (d) => { state.div = d; closeTeam(); render(); });
  pills($("splits"), SPLITS.map(([k, l], i) => [i, l]), (i) => i === state.split,
        (i) => { state.split = i; render(); });
  for (const [scope, id] of [["equipo", "markets-team"], ["partido", "markets-match"]]) {
    pills($(id), DATA.markets.filter((m) => m.scope === scope).map((m) => [m.key, m.label]),
          (k) => k === state.market, (k) => { state.market = k; state.sort = -1; render(); });
  }
}

function cellNode(c) {
  if (!c || c[0] == null) return el("div", { class: "cell vacia", text: "sin datos" });
  const [p, pl, nCur, nPrev, nW, label] = c;
  const gap = pts(p, pl);
  return el("div", { class: `cell ${label}`, title: LABELS[label] || "" },
    el("span", { class: "p", text: pct(p) }),
    el("span", { class: "l" },
      `liga ${pct(pl)}`,
      gap == null ? null : el("span", { class: "gap", text: ` · ${signed(gap)} pts` })),
    el("span", { class: "n", text: `${nCur} + ${nPrev} partidos · n ${num(nW)}` }),
    label === "insuficiente" ? el("span", { class: "n", text: "muestra insuficiente" }) : null);
}

function sortKey(team, lineIdx) {
  const c = team.r[state.market][lineIdx][state.split];
  if (!c || c[0] == null) return -Infinity;
  if (c[5] === "insuficiente") return -1e9 + (c[0] || 0);   // al final, sin mezclarse
  return state.byGap ? (pts(c[0], c[1]) ?? -Infinity) : c[0];
}

function renderTable() {
  const lg = DATA.leagues[state.div];
  const m = marketMeta(state.market);
  $("table-title").textContent =
    `${lg.name} · ${m.label} ${m.scope === "partido" ? "(total del partido)" : "(del equipo)"} · ` +
    `${SPLITS[state.split][1]} · más de…`;

  const teams = [...lg.teams];
  if (state.sort < 0) {
    teams.sort((a, b) => a.team.localeCompare(b.team, "es"));
  } else {
    teams.sort((a, b) => sortKey(b, state.sort) - sortKey(a, state.sort));
  }

  const head = el("tr", {},
    el("th", {}, el("button", { text: "Equipo", "data-active": String(state.sort < 0),
                                onclick: () => { state.sort = -1; render(); } })),
    ...m.lines.map((ln, i) => el("th", {},
      el("button", { text: `+${ln}`, "data-active": String(state.sort === i),
                     "aria-label": `Más de ${ln}: ordenar de mayor a menor`,
                     onclick: () => { state.sort = i; render(); } }))));
  const body = teams.map((t) => el("tr", {},
    el("td", {},
      el("button", { class: "team", text: t.team, onclick: () => openTeam(t) }),
      el("span", { class: "team-sub", text: teamSub(t) })),
    ...m.lines.map((_, i) => el("td", {}, cellNode(t.r[state.market][i][state.split])))));
  $("grid").replaceChildren(el("thead", {}, head), el("tbody", {}, ...body));
}

function teamSub(t) {
  const prev = t.prev_div == null ? "sin temporada anterior"
    : t.prev_div === state.div ? `anterior: ${t.mp} p. (×${num(t.prev_w)})`
    : `anterior en ${DIV_NAMES[t.prev_div] || t.prev_div}: ${t.mp} p. (×${num(t.prev_w)})`;
  return `${t.mc} partidos · ${prev}`;
}

function openTeam(t) {
  const sec = $("team");
  const blocks = DATA.markets.map((m) => el("div", {},
    el("h3", { text: `${m.label} ${m.scope === "partido" ? "(total del partido)" : "(del equipo)"}` }),
    el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", { text: "Más de" }),
                         ...SPLITS.map(([, l]) => el("th", { text: l })))),
      el("tbody", {}, ...m.lines.map((ln, i) => el("tr", {},
        el("td", { text: `+${ln}` }),
        ...SPLITS.map((_, s) => el("td", {}, cellNode(t.r[m.key][i][s]))))))))));
  sec.replaceChildren(
    el("button", { class: "pill close", text: "Cerrar", onclick: closeTeam }),
    el("h2", { text: t.team }),
    el("p", { class: "muted", text: `${DATA.leagues[state.div].name} · ${teamSub(t)}` }),
    ...blocks);
  sec.hidden = false;
  sec.scrollIntoView({ behavior: "smooth", block: "start" });
}
function closeTeam() { $("team").hidden = true; }

function render() {
  renderControls();
  renderTable();
  writeHash();
}

async function main() {
  try {
    const res = await fetch("data/rates.json", { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    DATA = await res.json();
  } catch (e) {
    $("meta").textContent = `No se pudieron cargar los datos (${e.message}).`;
    return;
  }
  state.div = Object.keys(DATA.leagues).find((d) => DATA.leagues[d].teams.length) || "SP1";
  state.market = DATA.markets[0].key;
  readHash();
  const through = Object.values(DATA.leagues).map((l) => l.data_through).filter(Boolean).sort();
  $("meta").textContent = `Partidos hasta ${through[through.length - 1] || "—"} · ` +
    `${Object.values(DATA.leagues).reduce((n, l) => n + l.teams.length, 0)} equipos en 8 ligas`;
  $("generated").textContent = `Generado el ${DATA.generated_at.replace("T", " ")} UTC.`;
  $("gap-pts").textContent = String(Math.round(DATA.league_gap * 100));
  $("by-gap").addEventListener("change", (e) => { state.byGap = e.target.checked; render(); });
  window.addEventListener("hashchange", () => { readHash(); render(); });
  render();
}

main();
