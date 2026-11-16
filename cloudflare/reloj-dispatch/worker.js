// Reloj del repartidor: cada 5 minutos (Cron Trigger) hace POST a /dispatch de telegram-bot.
// Sustituye a los cron de GitHub, que llegaban 5-8 h tarde (auditoría 16-nov-2026).
//
// Secretos del Worker (nunca en el código): DISPATCH_URL (URL base de telegram-bot) y
// CLOUD_RUN_TOKEN (cabecera x-cloud-token). Se cargan con `wrangler secret put` desde los
// secretos de GitHub en cloudflare-reloj-deploy.yml.
// User-Agent propio para que el latido de /dispatch distinga este origen ("cloudflare").
// El log solo muestra el código HTTP, nunca la URL ni el token.

const USER_AGENT = "reloj-cloudflare/1";

export async function tick(env, fetchImpl = fetch) {
  if (!env.DISPATCH_URL || !env.CLOUD_RUN_TOKEN) {
    console.log("reloj: faltan secretos, no se llama");
    return 0;
  }
  const url = env.DISPATCH_URL.replace(/\/+$/, "") + "/dispatch";
  const resp = await fetchImpl(url, {
    method: "POST",
    headers: { "x-cloud-token": env.CLOUD_RUN_TOKEN, "user-agent": USER_AGENT },
    signal: AbortSignal.timeout(100_000),
  });
  console.log(`reloj: /dispatch → HTTP ${resp.status}`);
  return resp.status;
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(tick(env).catch((e) => console.log(`reloj: error ${e.name}`)));
  },
  // Sin rutas públicas útiles: cualquier visita recibe 404.
  async fetch() {
    return new Response("", { status: 404 });
  },
};
