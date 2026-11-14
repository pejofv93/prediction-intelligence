"use strict";
// Web de Tendencias. Vista "Equipos": porcentajes por equipo y mercado (data/rates.json, generado
// por trends_web/export_rates.py). Vista "Partidos": fichas de partido mp-v1 de los próximos días
// (data/previews.json, exportado de match_previews por sports-agent). Sin dependencias. Todo el
// texto entra por textContent.

const SPLITS = [["all", "Todos"], ["home", "En casa"], ["away", "Fuera"]];
const SAMPLES = [["all", "Toda la muestra"], ["cur", "Solo esta temporada"]];
const MIN_CUR_ONLY = 5;   // "solo esta temporada": por debajo, la celda sale en gris
const LABELS = { encima: "por encima", debajo: "por debajo", en_linea: "en línea con la liga",
                 insuficiente: "muestra insuficiente", sin_liga: "sin media de liga" };
const DIV_NAMES = { SP1: "LaLiga", SP2: "Segunda", I1: "Serie A", E0: "Premier", F1: "Ligue 1",
                    P1: "Primeira", D1: "Bundesliga", N1: "Eredivisie", E1: "Championship",
                    I2: "Serie B", F2: "Ligue 2", D2: "2. Bundesliga" };

const state = { view: "teams", div: null, split: 0, market: null, sample: "all", sort: -1, desc: false, byGap: false,
                day: null, match: null };
let DATA = null;
let PREV = null;          // data/previews.json
let PREV_ERROR = null;
let TEAMS_META = "";

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
// Ambos marcan / portería a cero: sí/no (línea 0,5 sobre 1/0) → columna "Sí" en vez de "+0,5".
const lineHead = (m, ln) => (m.binary ? "Sí" : `+${ln}`);

function readHash() {
  const raw = decodeURIComponent(location.hash.slice(1));
  if (raw === "partidos" || raw.startsWith("partidos/")) {
    state.view = "matches";
    state.match = raw.slice("partidos/".length) || null;
    return;
  }
  state.view = "teams";
  if (!DATA) return;
  const [div, split, market, sample] = raw.split("/");
  if (div && DATA.leagues[div]) state.div = div;
  state.sample = sample === "temporada" ? "cur" : "all";
  const s = SPLITS.findIndex(([k]) => k === split);
  if (s >= 0) state.split = s;
  if (market && marketMeta(market)) state.market = market;
}
function writeHash() {
  const h = state.view === "matches"
    ? `#partidos${state.match ? "/" + state.match : ""}`
    : `#${state.div}/${SPLITS[state.split][0]}/${state.market}${state.sample === "cur" ? "/temporada" : ""}`;
  if (location.hash !== h) history.replaceState(null, "", h);
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
  pills($("samples"), SAMPLES, (k) => k === state.sample,
        (k) => { state.sample = k; if ($("team").hidden === false) closeTeam(); render(); });
  $("by-gap").disabled = state.sample === "cur";     // sin media de liga en ese modo
  $("sample-note").hidden = state.sample !== "cur";
  for (const [scope, id] of [["equipo", "markets-team"], ["partido", "markets-match"]]) {
    pills($(id), DATA.markets.filter((m) => m.scope === scope).map((m) => [m.key, m.label]),
          (k) => k === state.market, (k) => { state.market = k; state.sort = -1; render(); });
  }
}

// "Solo esta temporada": porcentaje bruto con los partidos de este año (sin la anterior ni ajuste a la media).
function curOnly(c) {
  if (!c || c[2] == null || c[6] == null) return null;
  const [, , nCur, , , , hits] = c;
  return { p: nCur ? hits / nCur : null, hits, n: nCur, low: nCur < MIN_CUR_ONLY };
}

function cellNodeCur(c) {
  const k = curOnly(c);
  if (!k || !k.n) return el("div", { class: "cell vacia", text: k ? "sin partidos esta temporada" : "sin datos" });
  return el("div", { class: `cell actual${k.low ? " insuficiente" : ""}`,
                     title: k.low ? "muestra insuficiente" : "solo esta temporada" },
    el("span", { class: "p", text: pct(k.p) }),
    el("span", { class: "n", text: `${k.hits}/${k.n} partidos esta temporada` }),
    k.low ? el("span", { class: "n", text: "muestra insuficiente" }) : null);
}

function cellNode(c) {
  if (state.sample === "cur") return cellNodeCur(c);
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
  if (state.sample === "cur") {
    const k = curOnly(c);
    if (!k || k.p == null) return -Infinity;
    return k.low ? -1e9 + k.p : k.p;                         // gris al final, sin mezclarse
  }
  if (!c || c[0] == null) return -Infinity;
  if (c[5] === "insuficiente") return -1e9 + (c[0] || 0);   // al final, sin mezclarse
  return state.byGap ? (pts(c[0], c[1]) ?? -Infinity) : c[0];
}

function renderTable() {
  const lg = DATA.leagues[state.div];
  const m = marketMeta(state.market);
  $("table-title").textContent =
    `${lg.name} · ${m.label} ${m.scope === "partido" ? "(total del partido)" : "(del equipo)"} · ` +
    `${SPLITS[state.split][1]} · ${state.sample === "cur" ? "solo esta temporada · " : ""}` +
    (m.binary ? "% de partidos" : "más de…");

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
      el("button", { text: lineHead(m, ln), "data-active": String(state.sort === i),
                     "aria-label": `${m.binary ? m.label : "Más de " + ln}: ordenar de mayor a menor`,
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
      el("thead", {}, el("tr", {}, el("th", { text: m.binary ? "" : "Más de" }),
                         ...SPLITS.map(([, l]) => el("th", { text: l })))),
      el("tbody", {}, ...m.lines.map((ln, i) => el("tr", {},
        el("td", { text: lineHead(m, ln) }),
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

// ── Vista "Partidos": fichas de partido ─────────────────────────────────────
const STAT_NAMES = { goals: "Goles", corners: "Córners", cards: "Tarjetas", shots: "Remates",
                     sot: "Remates a puerta", fouls: "Faltas" };
const MP_TAGS = { "ventaja medida": "ventaja", "solo referencia": "referencia",
                  "muestra insuficiente": "insuficiente" };
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
const dayKey = (iso) => new Date(iso).toLocaleDateString("sv-SE");          // AAAA-MM-DD local
const dayLabel = (k) => new Date(`${k}T12:00:00`).toLocaleDateString("es-ES",
  { weekday: "short", day: "numeric", month: "short" });
const hhmm = (iso) => new Date(iso).toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
const leagueName = (div) => DIV_NAMES[div] || div;
const highlights = (m) => Object.values(m.stats).filter((s) => s.available)
  .reduce((n, s) => n + Object.values(s.blocks).filter((b) => b.highlight).length, 0);

function mpTag(label) { return el("span", { class: `tag ${MP_TAGS[label] || ""}`, text: label }); }

function renderDays() {
  const days = [...new Set(PREV.matches.map((m) => dayKey(m.kickoff_utc)))];
  if (!days.includes(state.day)) state.day = days[0] || null;
  pills($("days"), days.map((d) => [d, dayLabel(d)]), (d) => d === state.day,
        (d) => { state.day = d; state.match = null; render(); });
}

function renderMatchList() {
  const list = $("match-list");
  if (PREV_ERROR) { list.replaceChildren(el("p", { class: "muted", text: PREV_ERROR })); return; }
  if (!PREV.matches.length) {
    list.replaceChildren(el("p", { class: "muted",
      text: `No hay fichas para los próximos ${PREV.days} días (parón o jornada sin calcular).` }));
    return;
  }
  const todays = PREV.matches.filter((m) => dayKey(m.kickoff_utc) === state.day);
  const byDiv = new Map();
  for (const m of todays) byDiv.set(m.div, [...(byDiv.get(m.div) || []), m]);
  list.replaceChildren(...[...byDiv].map(([div, ms]) => el("div", { class: "match-group" },
    el("h3", { text: leagueName(div) }),
    ...ms.map((m) => {
      const n = highlights(m);
      return el("button", { class: `match-row${m.id === state.match ? " on" : ""}`,
                            onclick: () => { state.match = m.id; render(); scrollToMatch(); } },
        el("span", { class: "ko", text: hhmm(m.kickoff_utc) }),
        el("span", { class: "teams", text: `${m.home} – ${m.away}` }),
        m.low_sample ? mpTag("muestra insuficiente")
          : n ? el("span", { class: "tag ventaja", text: `${n} con ventaja medida` }) : null);
    }))));
}

function sampleText(m, side) {
  const s = m.sample[side] || {};
  return `${m[side]}: ${s.n_cur ?? "—"} partidos esta temporada · n efectivo ${num(s.n_eff)}`;
}

function linesTable(title, block) {
  return el("div", { class: "block" },
    el("p", { class: "block-title" }, `${title} `, mpTag(block.label)),
    el("table", { class: "lines" },
      el("thead", {}, el("tr", {}, ...["Más de", "Modelo", "Liga", "Dif.", "Muestra liga"]
        .map((t) => el("th", { text: t })))),
      el("tbody", {}, ...block.lines.map(([ln, pm, pl, nl]) => {
        const gap = pts(pm, pl);
        const cls = gap == null || Math.abs(gap) < 5 ? "" : gap > 0 ? "up" : "down";
        return el("tr", { class: block.highlight ? "hl" : "" },
          el("td", { text: `+${ln}` }),
          el("td", { class: "pm", text: pct(pm) }),
          el("td", { text: pct(pl) }),
          el("td", { class: `gap ${cls}`, text: gap == null ? "—" : `${signed(gap)} pts` }),
          el("td", { class: "muted", text: nl == null ? "—" : num(nl) }));
      }))));
}

function renderMatch() {
  const sec = $("match");
  const m = PREV && PREV.matches.find((x) => x.id === state.match);
  if (!m) { sec.hidden = true; return; }
  const kickoff = new Date(m.kickoff_utc).toLocaleString("es-ES",
    { weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
  const stats = Object.keys(STAT_NAMES).map((k) => {
    const s = m.stats[k];
    if (!s || !s.available) return el("div", { class: "stat" },
      el("h3", { text: STAT_NAMES[k] }), el("p", { class: "muted", text: "Sin datos de la liga." }));
    const [eh, ea, et] = s.exp;
    const [lh, la] = s.league_mean;
    return el("div", { class: "stat" },
      el("h3", { text: STAT_NAMES[k] }),
      el("div", { class: "table-wrap" }, el("table", { class: "exp" },
        el("thead", {}, el("tr", {}, el("th", { text: "" }), el("th", { text: m.home }),
                           el("th", { text: m.away }), el("th", { text: "Total" }))),
        el("tbody", {},
          el("tr", {}, el("td", { text: "Esperado" }),
             ...[eh, ea, et].map((x) => el("td", { class: "pm", text: num(x) }))),
          el("tr", { class: "muted" }, el("td", { text: "Media de la liga" }),
             ...[lh, la, lh + la].map((x) => el("td", { text: num(x) })))))),
      el("div", { class: "blocks" },
        linesTable(m.home, s.blocks.home), linesTable(m.away, s.blocks.away),
        linesTable("Total del partido", s.blocks.total)));
  });
  sec.replaceChildren(...[    // replaceChildren pintaría un null como texto "null"
    el("button", { class: "pill close", text: "Cerrar", onclick: () => { state.match = null; render(); } }),
    el("h2", { text: `${m.home} – ${m.away}` }),
    el("p", { class: "muted", text: `${leagueName(m.div)} · ${kickoff} (${TZ}) · datos hasta ${m.data_cutoff || "—"}` }),
    el("p", { class: "muted", text: sampleText(m, "home") }),
    el("p", { class: "muted", text: sampleText(m, "away") }),
    m.low_sample ? el("p", { class: "warn",
      text: "Muestra insuficiente: algún equipo tiene pocos partidos esta temporada. Se calcula, pero no se resalta." }) : null,
    ...stats].filter(Boolean));
  sec.hidden = false;
}
function scrollToMatch() { $("match").scrollIntoView({ behavior: "smooth", block: "start" }); }

function renderView() {
  const teams = state.view === "teams";
  $("view-teams").hidden = !teams;
  $("view-matches").hidden = teams;
  $("tab-teams").setAttribute("aria-selected", String(teams));
  $("tab-matches").setAttribute("aria-selected", String(!teams));
  $("meta").textContent = teams ? TEAMS_META
    : PREV ? `${PREV.matches.length} fichas · próximos ${PREV.days} días` : "";
}

function render() {
  renderView();
  if (state.view === "teams") {
    if (DATA) { renderControls(); renderTable(); }
  } else if (PREV || PREV_ERROR) {
    if (PREV) {
      const sel = PREV.matches.find((x) => x.id === state.match);
      if (sel) state.day = dayKey(sel.kickoff_utc); else state.match = null;
      renderDays();
    }
    renderMatchList();
    renderMatch();
  }
  writeHash();
}

async function load(path) {
  const res = await fetch(path, { cache: "no-cache" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function main() {
  const [rates, previews] = await Promise.allSettled([load("data/rates.json"), load("data/previews.json")]);
  if (rates.status === "fulfilled") {
    DATA = rates.value;
    state.div = Object.keys(DATA.leagues).find((d) => DATA.leagues[d].teams.length) || "SP1";
    state.market = DATA.markets[0].key;
    const through = Object.values(DATA.leagues).map((l) => l.data_through).filter(Boolean).sort();
    TEAMS_META = `Partidos hasta ${through[through.length - 1] || "—"} · ` +
      `${Object.values(DATA.leagues).reduce((n, l) => n + l.teams.length, 0)} equipos en 8 ligas`;
    $("generated").textContent = `Generado el ${DATA.generated_at.replace("T", " ")} UTC.`;
    $("gap-pts").textContent = String(Math.round(DATA.league_gap * 100));
  } else {
    TEAMS_META = `No se pudieron cargar los datos (${rates.reason.message}).`;
  }
  if (previews.status === "fulfilled") {
    PREV = previews.value;
    $("min-cur").textContent = String(PREV.min_cur);
    $("mp-version").textContent = PREV.model_version;
    $("mp-generated").textContent = `Exportado el ${PREV.generated_at.slice(0, 16).replace("T", " ")} UTC.`;
  } else {
    PREV_ERROR = `No se pudieron cargar las fichas (${previews.reason.message}).`;
  }
  readHash();
  $("by-gap").addEventListener("change", (e) => { state.byGap = e.target.checked; render(); });
  $("tab-teams").addEventListener("click", () => { state.view = "teams"; render(); });
  $("tab-matches").addEventListener("click", () => { state.view = "matches"; render(); });
  window.addEventListener("hashchange", () => { readHash(); render(); });
  render();
}

main();
