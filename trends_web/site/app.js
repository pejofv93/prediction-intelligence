"use strict";
// Web de Tendencias. Vista "Equipos": porcentajes por equipo y mercado (data/rates.json, generado
// por trends_web/export_rates.py). Vista "Partidos": fichas de partido mp-v1 de los próximos días
// (data/previews.json, exportado de match_previews por sports-agent). Sin dependencias. Todo el
// texto entra por textContent.

const SPLITS = [["all", "Todos"], ["home", "En casa"], ["away", "Fuera"]];
const LABELS = { encima: "por encima", debajo: "por debajo", en_linea: "en línea con la liga",
                 insuficiente: "muestra insuficiente", sin_liga: "sin media de liga" };
const DIV_NAMES = { SP1: "LaLiga", SP2: "Segunda", I1: "Serie A", E0: "Premier", F1: "Ligue 1",
                    P1: "Primeira", D1: "Bundesliga", N1: "Eredivisie", E1: "Championship",
                    I2: "Serie B", F2: "Ligue 2", D2: "2. Bundesliga",
                    // Femenino (mp-w1, Highlightly)
                    WLF: "Liga F", WSL: "WSL", WFBL: "Frauen-Bundesliga", WSA: "Serie A fem.",
                    WD1: "D1 Arkema" };

const state = { view: "teams", div: null, split: 0, market: null, sort: -1, desc: false, byGap: false,
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

// Nombre de pantalla en español ("Ath Bilbao" → "Athletic"): lo pone export_rates.py desde
// shared/team_display_names.py; un rates.json antiguo sin "name" enseña el del CSV.
const teamName = (t) => t.name || t.team;

function marketMeta(key) { return DATA.markets.find((m) => m.key === key); }
// Ambos marcan / portería a cero: sí/no (línea 0,5 sobre 1/0) → columna "Sí" en vez de "+0,5".
const lineHead = (m, ln) => (m.binary ? "Sí" : `+${ln}`);

function readHash() {
  const raw = decodeURIComponent(location.hash.slice(1));
  if (raw === "partidos" || raw.startsWith("partidos/")) {
    state.view = "matches";
    const rest = raw.slice("partidos/".length);
    // #partidos/AAAA-MM-DD = ese día (enlace del resumen de Telegram); si no, id de una ficha
    if (/^\d{4}-\d{2}-\d{2}$/.test(rest)) { state.day = rest; state.match = null; }
    else state.match = rest || null;
    return;
  }
  state.view = "teams";
  if (!DATA) return;
  const [div, split, market] = raw.split("/");
  if (div && DATA.leagues[div]) state.div = div;
  const s = SPLITS.findIndex(([k]) => k === split);
  if (s >= 0) state.split = s;
  if (market && marketMeta(market)) state.market = market;
}
function writeHash() {
  const h = state.view === "matches"
    ? `#partidos${state.match ? "/" + state.match : ""}`
    : `#${state.div}/${SPLITS[state.split][0]}/${state.market}`;
  // history.state se conserva: marca la entrada que añadió openMatch (para cerrar con "atrás").
  if (location.hash !== h) history.replaceState(history.state, "", h);
}

// Ficha abierta desde la lista = una entrada nueva en el historial, para que el botón atrás del
// móvil la cierre. Cambiar de ficha con otra abierta no añade entradas.
function openMatch(id) {
  if (!state.match) history.pushState({ ficha: true }, "", `#partidos/${encodeURIComponent(id)}`);
  state.match = id;
  render();
  scrollToMatch();
}
// Cerrar (botón, Escape o pulsar fuera): si la entrada es de openMatch, se vuelve atrás (lo mismo
// que el botón atrás); si se entró con un enlace directo a la ficha, se cierra sin salir de la web.
function closeMatch() {
  if (!state.match) return;
  if (history.state && history.state.ficha) { history.back(); return; }
  state.match = null;
  render();
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

// Un solo porcentaje por celda. El modelo mezcla por dentro la temporada anterior (decisión del
// usuario, 19-nov-2026: no se enseña); la muestra que se ve es la de esta temporada.
function cellNode(c) {
  if (!c || c[0] == null) return el("div", { class: "cell vacia", text: "sin datos" });
  const [p, pl, nCur, , , label] = c;
  const gap = pts(p, pl);
  return el("div", { class: `cell ${label}`, title: LABELS[label] || "" },
    el("span", { class: "p", text: pct(p) }),
    el("span", { class: "l" },
      `liga ${pct(pl)}`,
      gap == null ? null : el("span", { class: "gap", text: ` · ${signed(gap)} pts` })),
    el("span", { class: "n", text: `${nCur} partidos esta temporada` }),
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
    `${SPLITS[state.split][1]} · ` +
    (m.binary ? "% de partidos" : "más de…");

  const teams = [...lg.teams];
  if (state.sort < 0) {
    teams.sort((a, b) => teamName(a).localeCompare(teamName(b), "es"));
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
      el("button", { class: "team", text: teamName(t), onclick: () => openTeam(t) }),
      el("span", { class: "team-sub", text: teamSub(t) })),
    ...m.lines.map((_, i) => el("td", {}, cellNode(t.r[state.market][i][state.split])))));
  $("grid").replaceChildren(el("thead", {}, head), el("tbody", {}, ...body));
}

function teamSub(t) { return `${t.mc} partidos esta temporada`; }

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
    el("h2", { text: teamName(t) }),
    el("p", { class: "muted", text: `${DATA.leagues[state.div].name} · ${teamSub(t)}` }),
    ...blocks);
  sec.hidden = false;
  sec.scrollIntoView({ behavior: "smooth", block: "start" });
}
function closeTeam() { $("team").hidden = true; }

// ── Vista "Partidos": fichas de partido ─────────────────────────────────────
const STAT_NAMES = { corners: "Córners", cards: "Tarjetas", fouls: "Faltas", shots: "Remates",
                     sot: "Remates a puerta", goals: "Goles" };
const MP_TAGS = { "ventaja medida": "ventaja", "solo referencia": "referencia", "medido en backtest": "backtest",
                  "muestra insuficiente": "insuficiente" };
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
const dayKey = (iso) => new Date(iso).toLocaleDateString("sv-SE");          // AAAA-MM-DD local
const dayLabel = (k) => new Date(`${k}T12:00:00`).toLocaleDateString("es-ES",
  { weekday: "short", day: "numeric", month: "short" });
const hhmm = (iso) => new Date(iso).toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
const isWomen = (m) => m.group === "women";
const isCup = (m) => m.group === "cups";
const leagueName = (div) => DIV_NAMES[div] || div;
const groupTitle = (m) => (isWomen(m) ? `Femenino · ${leagueName(m.div)}`
  : isCup(m) ? `Copas · ${m.cup || "copa"}` : leagueName(m.div));
const groupKey = (m) => (isCup(m) ? `cup:${m.cup}` : m.div);   // una copa no se mezcla con su liga
// Línea destacada: como mucho UNA por mercado y lado (local, visitante, total), la que más se
// separa de la liga, y solo si llega a STANDOUT_PTS puntos (los que se ven en la ficha) en un
// mercado con ventaja medida (block.highlight, que ya es falso con muestra insuficiente).
// Las líneas de un mismo mercado se encienden juntas: contarlas todas inflaría la marca.
const STANDOUT_PTS = 10;
function standoutLine(block) {
  if (!block.highlight) return null;
  let best = null;
  for (const [ln, pm, pl] of block.lines) {
    const gap = pts(pm, pl);
    if (gap != null && Math.abs(gap) >= STANDOUT_PTS && (!best || Math.abs(gap) > Math.abs(best.gap)))
      best = { ln, gap };
  }
  return best;
}
const standoutCount = (m) => Object.values(m.stats).filter((s) => s.available)
  .reduce((n, s) => n + Object.values(s.blocks).filter((b) => standoutLine(b)).length, 0);
const standoutText = (n) => `${n} ${n === 1 ? "línea destacada" : "líneas destacadas"}`;

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
  // Primero las ligas masculinas, después "Copas" y al final "Femenino", cada una en orden de hora.
  const men = todays.filter((x) => !isWomen(x) && !isCup(x));
  for (const m of [...men, ...todays.filter(isCup), ...todays.filter(isWomen)])
    byDiv.set(groupKey(m), [...(byDiv.get(groupKey(m)) || []), m]);
  list.replaceChildren(...[...byDiv].map(([div, ms]) => el("div", { class: "match-group" },
    el("h3", { text: groupTitle(ms[0]) }),
    ...ms.map((m) => {
      const n = standoutCount(m);
      return el("button", { class: `match-row${m.id === state.match ? " on" : ""}`,
                            onclick: () => openMatch(m.id) },
        el("span", { class: "ko", text: hhmm(m.kickoff_utc) }),
        el("span", { class: "teams", text: `${m.home} – ${m.away}` }),
        m.low_sample ? mpTag("muestra insuficiente")
          : n ? el("span", { class: "tag ventaja", text: standoutText(n) }) : null);
    }))));
}

function sampleText(m, side) {
  const s = m.sample[side] || {};
  return `${m[side]}: ${s.n_cur ?? "—"} partidos esta temporada`;
}

// Línea principal (P(más) más cercana al 50 %): la exporta sports-agent; si falta, se calcula.
function mainLine(block) {
  if (block.main != null) {
    const hit = block.lines.find(([ln]) => ln === block.main);
    if (hit) return hit;
  }
  return block.lines.reduce((b, l) => (!b || Math.abs(l[1] - 0.5) < Math.abs(b[1] - 0.5) ? l : b), null);
}

function linesTable(title, block) {
  const best = standoutLine(block);
  return el("div", { class: "block" },
    el("p", { class: "block-title" }, `${title} `, mpTag(block.label)),
    el("table", { class: "lines" },
      el("thead", {}, el("tr", {}, ...["Más de", "Modelo", "Liga", "Dif.", "Muestra liga"]
        .map((t) => el("th", { text: t })))),
      el("tbody", {}, ...block.lines.map(([ln, pm, pl, nl]) => {
        const gap = pts(pm, pl);
        const cls = gap == null || Math.abs(gap) < 5 ? "" : gap > 0 ? "up" : "down";
        const star = Boolean(best) && best.ln === ln;
        return el("tr", { class: [block.highlight ? "hl" : "", star ? "star" : ""].join(" ").trim(),
                          title: star ? `Destacada: modelo a ${STANDOUT_PTS} puntos o más de la liga` : "" },
          el("td", { text: `${star ? "★ " : ""}+${ln}` }),
          el("td", { class: "pm", text: pct(pm) }),
          el("td", { text: pct(pl) }),
          el("td", { class: `gap ${cls}`, text: gap == null ? "—" : `${signed(gap)} pts` }),
          el("td", { class: "muted", text: nl == null ? "—" : num(nl) }));
      }))));
}

// ── Ficha "Duelo" (propuesta C, 19-nov-2026) ─────────────────────────────────
// Escudos: círculo de color con iniciales (nada de logos reales). El color sale del nombre.
// Nombre e iniciales llegan ya en previews.json (shared/team_display_names.py: m.home/m.away de
// pantalla y m.badges); initials() es el respaldo para un previews.json antiguo y sigue el mismo
// criterio que badge_initials() en Python (lo comprueba trends_web/tests/initials.test.mjs).
const BADGE_COLORS = ["#B42318", "#1D4ED8", "#7C3AED", "#B45309", "#0F766E", "#334155", "#BE185D", "#4D7C0F"];
function badgeColor(name, avoid) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  let c = BADGE_COLORS[h % BADGE_COLORS.length];
  if (c === avoid) c = BADGE_COLORS[(h + 3) % BADGE_COLORS.length];
  return c;
}
function initials(name) {
  let w = name.replace(/[^\p{L}\p{N} ]/gu, " ").split(/\s+/).filter(Boolean);
  if (!w.length) return "?";
  if (w.length > 1) {                       // sin partículas en minúscula: "Sporting de Portugal" → SP
    const big = w.filter((x) => x !== x.toLowerCase() || x === x.toUpperCase());
    if (big.length) w = big;
  }
  return (w.length === 1 ? w[0].slice(0, 3) : w.slice(0, 3).map((x) => x[0]).join("")).toUpperCase();
}

// Píldoras "Tendencias del partido" (lo mismo en Telegram): SOLO mercados por equipo con "ventaja
// medida" (en copas y femenino, además 50 graduadas en directo con IC95 bajo 0). En cada mercado y
// equipo, la línea MÁS ALTA con el modelo entre el 60 % y el 90 %, sin las de 0,5 salvo "marca al
// menos 1 gol"; como mucho 5, de mayor a menor %. Todas con ⭐: el modelo acierta más que la liga.
const PILL_MIN = 0.60, PILL_MAX = 0.90, PILL_MAX_N = 5;
const PILL_NOUN = { corners: "córners", cards: "tarjetas", fouls: "faltas", shots: "remates",
                    sot: "remates a puerta", goals: "goles" };
const STAR_LEGEND = "⭐ = el modelo acierta más que la media de la liga";
function pillBody(team, stat, ln) {
  return stat === "goals" && ln === 0.5
    ? [`${team} `, el("b", { text: "marca" }), " al menos 1 gol"]
    : [`${team} más de `, el("b", { text: num(ln) }), ` ${PILL_NOUN[stat]}`];
}
function matchPills(m) {
  if (m.low_sample) return [];
  const out = [];
  for (const stat of Object.keys(STAT_NAMES)) {
    const s = m.stats[stat];
    if (!s || !s.available) continue;
    for (const side of ["home", "away"]) {
      const blk = s.blocks[side] || {};
      if (blk.label !== "ventaja medida") continue;
      const ok = (blk.lines || []).filter(([ln, pm]) => pm >= PILL_MIN && pm <= PILL_MAX && (ln !== 0.5 || stat === "goals"));
      if (!ok.length) continue;
      const [ln, pm] = ok.reduce((x, y) => (y[0] > x[0] ? y : x));
      out.push({ side, body: pillBody(m[side], stat, ln), p: pm });
    }
  }
  return out.sort((a, b) => b.p - a.p).slice(0, PILL_MAX_N);
}

function sideLine(block) {
  const ml = mainLine(block);
  return ml ? [`+${num(ml[0])}`, pct(ml[1])] : ["—", "—"];
}

function marketCard(m, k, colors) {
  const s = m.stats[k];
  if (!s || !s.available) return el("section", { class: "mcard" },
    el("h3", { text: STAT_NAMES[k] }), el("p", { class: "muted", text: "Sin datos de la liga." }));
  const [eh, ea, et] = s.exp;
  const [lh, la] = s.league_mean;
  const star = ["total", "home", "away"].some((b) => s.blocks[b] && standoutLine(s.blocks[b]));
  const [hl, hp] = sideLine(s.blocks.home), [al, ap] = sideLine(s.blocks.away), [tl, tp] = sideLine(s.blocks.total);
  const share = eh + ea > 0 ? Math.round((eh / (eh + ea)) * 100) : 50;
  return el("section", { class: "mcard" },
    el("div", { class: "mrow" },
      el("div", { class: "side" },
        el("span", { class: "exp", text: num(Number(eh).toFixed(1)) }),
        el("span", { class: "ln" }, `${hl} · `, el("strong", { text: hp }))),
      el("div", { class: "mid" },
        el("h3", { text: `${STAT_NAMES[k]}${star ? " ★" : ""}`,
                   title: star ? "Alguna línea de este mercado está destacada (ver más líneas)" : "" }),
        el("span", { class: "ln" }, `Total ${num(Number(et).toFixed(1))} · ${tl} `, el("strong", { text: tp }))),
      el("div", { class: "side right" },
        el("span", { class: "exp", text: num(Number(ea).toFixed(1)) }),
        el("span", { class: "ln" }, `${al} · `, el("strong", { text: ap })))),
    el("div", { class: "bar", role: "img", "aria-label": `Reparto del esperado: ${m.home} ${share} %, ${m.away} ${100 - share} %` },
      el("span", { style: `width:${share}%;background:${colors[0]}` }),
      el("span", { style: `width:${100 - share}%;background:${colors[1]}` })),
    el("details", { class: "more" },
      el("summary", { text: "Más líneas" }),
      el("p", { class: "muted", text: `Media de la liga: ${m.home} ${num(lh)} · ${m.away} ${num(la)} · total ${num(lh + la)}` }),
      el("div", { class: "blocks" },
        linesTable("Total del partido", s.blocks.total),
        linesTable(m.home, s.blocks.home), linesTable(m.away, s.blocks.away))));
}

function teamSide(name, n, color, badge) {
  return el("div", { class: "duel-team" },
    el("div", { class: "badge", "aria-hidden": "true", style: `background:${color}`, text: badge || initials(name) }),
    el("div", { class: "duel-name", text: name }),
    el("div", { class: "duel-sub", text: `${n ?? "—"} partidos esta temporada` }));
}

function renderMatch() {
  const sec = $("match");
  const m = PREV && PREV.matches.find((x) => x.id === state.match);
  if (!m) { sec.hidden = true; return; }
  const ko = new Date(m.kickoff_utc);
  const c1 = badgeColor(m.home), c2 = badgeColor(m.away, c1);
  const pills = m.low_sample ? [] : matchPills(m);
  sec.replaceChildren(...[    // replaceChildren pintaría un null como texto "null"
    el("div", { class: "duel" },
      el("button", { class: "pill close", text: "Cerrar", "aria-label": "Cerrar la ficha", onclick: closeMatch }),
      el("div", { class: "duel-grid" },
        teamSide(m.home, (m.sample.home || {}).n_cur, c1, (m.badges || [])[0]),
        el("div", { class: "duel-mid" },
          el("div", { class: "duel-sub", text: groupTitle(m) }),
          el("div", { class: "duel-time", text: hhmm(m.kickoff_utc) }),
          el("div", { class: "duel-sub", text: ko.toLocaleDateString("es-ES", { weekday: "long", day: "numeric", month: "short" }) })),
        teamSide(m.away, (m.sample.away || {}).n_cur, c2, (m.badges || [])[1]))),
    isWomen(m) ? el("p", { class: "warn",
      text: `Fútbol femenino (modelo ${m.model_version}, datos de Highlightly): sin ⭐ hasta 50 partidos graduados en directo con IC95 entero bajo 0, como en copas.` }) : null,
    isCup(m) ? el("p", { class: "warn",
      text: `Copa (modelo de clubes, ${m.model_version}): cada equipo con su fuerza en ${leagueName(m.div)}. ` +
            "\"Medido en backtest\" en córners, faltas, goles, remates y a puerta por equipo; sin estrellas hasta medirlo en directo." }) : null,
    m.low_sample ? el("p", { class: "warn",
      text: "Muestra insuficiente: algún equipo tiene pocos partidos esta temporada. Se calcula, pero no se resalta." }) : null,
    el("section", { class: "trends", "aria-label": "Tendencias del partido" },
      el("div", { class: "trends-head" },
        el("h2", { text: "Tendencias del partido" }),
        el("span", { class: "muted", text: `${STAR_LEGEND}. De más a menos probable` })),
      pills.length
        ? el("div", { class: "pills" }, ...pills.map((x) => el("span", { class: "tpill star",
            title: "Mercado con ventaja medida: el modelo acierta más que la media de la liga" },
            el("span", { class: "dot", "aria-hidden": "true", style: `background:${x.side === "home" ? c1 : c2}` }),
            el("span", {}, "⭐ ", ...x.body), el("strong", { text: pct(x.p) }))))
        : el("p", { class: "muted", text: m.low_sample ? "Sin tendencias: muestra insuficiente."
            : "Ningún mercado con ventaja medida entre el 60 % y el 90 % en este partido." })),
    el("p", { class: "hint", text: "Número esperado de cada equipo, su línea y el % de que haya MÁS. En el centro, el total del partido." }),
    ...Object.keys(STAT_NAMES).map((k) => marketCard(m, k, [c1, c2])),
    el("p", { class: "hint", text: `Datos hasta ${m.data_cutoff || "—"}. Solo estadísticas del modelo. Sin cuotas ni recomendaciones.` }),
  ].filter(Boolean));
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
  window.addEventListener("popstate", () => { readHash(); render(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.view === "matches" && state.match) closeMatch();
  });
  // Pulsar fuera de la ficha la cierra; otra fila de la lista cambia de ficha en vez de cerrar.
  document.addEventListener("click", (e) => {
    if (state.view !== "matches" || !state.match || !e.target.isConnected) return;
    if ($("match").contains(e.target) || e.target.closest(".match-row")) return;
    closeMatch();
  });
  render();
}

main();
