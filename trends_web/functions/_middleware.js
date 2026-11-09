// Protección de la web de Tendencias en Cloudflare Pages: se ejecuta ANTES de servir cualquier
// ruta (la página, app.js, data/rates.json…). Autenticación básica con TRENDS_WEB_USER y
// TRENDS_WEB_PASS, secretos del proyecto de Pages (nunca en el repositorio).
// Falla cerrada: si falta cualquiera de los dos, deniega TODO (503), también con credenciales.

const REALM = 'Basic realm="Tendencias", charset="UTF-8"';
const PRIVATE_HEADERS = {
  "Cache-Control": "private, no-store",
  "X-Robots-Tag": "noindex, nofollow",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
};

function deny(status, text, extra = {}) {
  return new Response(text, {
    status,
    headers: { "Content-Type": "text/plain; charset=utf-8", ...PRIVATE_HEADERS, ...extra },
  });
}

async function sha256(text) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
}

// Comparación en tiempo constante: se comparan los resúmenes SHA-256 (misma longitud siempre).
async function sameSecret(a, b) {
  const [x, y] = await Promise.all([sha256(a), sha256(b)]);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

export function parseBasic(header) {
  const m = /^Basic\s+([A-Za-z0-9+/=]+)\s*$/i.exec(header || "");
  if (!m) return null;
  let decoded;
  try {
    decoded = new TextDecoder().decode(Uint8Array.from(atob(m[1]), (c) => c.charCodeAt(0)));
  } catch {
    return null;
  }
  const i = decoded.indexOf(":");
  return i < 0 ? null : { user: decoded.slice(0, i), pass: decoded.slice(i + 1) };
}

export async function onRequest(context) {
  const { request, env, next } = context;
  const user = env.TRENDS_WEB_USER;
  const pass = env.TRENDS_WEB_PASS;
  if (!user || !pass) {
    return deny(503, "Web no disponible: falta configurar el acceso.");
  }
  const cred = parseBasic(request.headers.get("Authorization"));
  // Siempre se hacen las dos comparaciones, para no delatar cuál falla.
  const okUser = await sameSecret(cred ? cred.user : "", user);
  const okPass = await sameSecret(cred ? cred.pass : "", pass);
  if (!cred || !okUser || !okPass) {
    return deny(401, "Acceso restringido.", { "WWW-Authenticate": REALM });
  }
  const res = await next();
  const out = new Response(res.body, res);
  for (const [k, v] of Object.entries(PRIVATE_HEADERS)) out.headers.set(k, v);
  return out;
}
