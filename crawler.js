const { chromium } = require('playwright');

const RELAY_URL = process.env.RELAY_URL;
const RELAY_SECRET = process.env.RELAY_SECRET;
const KUMA_URL = process.env.KUMA_URL;

if (!RELAY_URL || !RELAY_SECRET || !KUMA_URL) {
  console.error('RELAY_URL, RELAY_SECRET et KUMA_URL sont requis (env vars).');
  process.exit(1);
}

async function relayGet(path) {
  const res = await fetch(`${RELAY_URL}${path}`, {
    headers: { 'x-relay-secret': RELAY_SECRET },
  });
  const data = await res.json();
  if (!res.ok || data?.success !== true) {
    throw new Error(`Relay error on ${path}: ${data?.error || res.status}`);
  }
  return data;
}

async function relayPost(path) {
  const res = await fetch(`${RELAY_URL}${path}`, {
    method: 'POST',
    headers: { 'x-relay-secret': RELAY_SECRET },
  });
  const data = await res.json();
  if (!res.ok || data?.success !== true) {
    throw new Error(`Relay error on ${path}: ${data?.error || res.status}`);
  }
  return data;
}

async function pushStatus(pushToken, status, msg, pingMs) {
  if (!pushToken) {
    console.error('[push] pushToken manquant, envoi ignoré pour ce monitor.');
    return;
  }
  // ping = temps de chargement mesuré par Playwright (ms), envoyé à Kuma qui
  // l'affiche nativement (avgPing, graphique de temps de réponse) exactement
  // comme s'il s'agissait d'un ping de monitoring HTTP classique.
  const pingParam = Number.isFinite(pingMs) ? `&ping=${Math.round(pingMs)}` : '';
  const url = `${KUMA_URL}/api/push/${pushToken}?status=${status}&msg=${encodeURIComponent(msg)}${pingParam}`;
  try {
    const res = await fetch(url);
    if (!res.ok) {
      console.error(`[push] échec HTTP ${res.status} pour token ${pushToken.slice(0, 6)}...`);
    }
  } catch (e) {
    console.error(`[push] erreur réseau pour token ${pushToken.slice(0, 6)}...:`, e.message);
  }
}

// Détermine si un site doit être re-vérifié à ce run, en fonction de sa
// fréquence choisie (crawl_interval_minutes) et de la dernière vérification
// (last_crawled_at). Si jamais crawlé, on le considère dû immédiatement.
function isDue(site) {
  if (!site.last_crawled_at) return true;
  const intervalMs = (site.crawl_interval_minutes ?? 1440) * 60 * 1000;
  const nextDue = new Date(site.last_crawled_at).getTime() + intervalMs;
  return Date.now() >= nextDue;
}

// Vérifie une page : navigation OK + pas d'erreur API silencieuse en arrière-plan
// (c'est le but même du projet : détecter un backend qui répond en erreur
// alors que le frontend a l'air fonctionnel).
async function checkPage(context, url) {
  const page = await context.newPage();

  const apiErrors = [];
  const consoleErrors = [];

  page.on('response', (response) => {
    const req = response.request();
    const type = req.resourceType();
    // On ne surveille que les appels XHR/fetch (appels API), pas les assets
    // statiques (images, css, fonts) qui peuvent 404 sans que ce soit critique.
    if ((type === 'xhr' || type === 'fetch') && response.status() >= 500) {
      apiErrors.push(`${response.status()} ${req.url()}`);
    }
  });

  page.on('pageerror', (err) => {
    consoleErrors.push(err.message);
  });

  let status = 'up';
  let msg = 'OK';
  let loadTimeMs = null;
  const navStart = Date.now();

  try {
    const response = await page.goto(url, { waitUntil: 'networkidle', timeout: 20000 });

    if (!response || !response.ok()) {
      status = 'down';
      msg = `HTTP ${response ? response.status() : 'no response'}`;
    } else if (apiErrors.length > 0) {
      status = 'down';
      msg = `Erreur API: ${apiErrors[0]}`;
    } else if (consoleErrors.length > 0) {
      status = 'down';
      msg = `Erreur JS: ${consoleErrors[0]}`;
    }

    // Temps de chargement réel mesuré par le navigateur (Navigation Timing
    // API) : de navigationStart à loadEventEnd. Plus fiable qu'un simple
    // chrono Node, car c'est la même mesure que le navigateur utilise en
    // interne. En repli, on utilise le chrono Node si l'API n'est pas
    // exploitable (page fermée trop vite, navigation échouée, etc).
    try {
      const timing = await page.evaluate(() => {
        const [nav] = performance.getEntriesByType('navigation');
        if (nav && nav.loadEventEnd > 0) return nav.loadEventEnd;
        return null;
      });
      loadTimeMs = timing != null ? timing : Date.now() - navStart;
    } catch {
      loadTimeMs = Date.now() - navStart;
    }
  } catch (err) {
    status = 'down';
    msg = String(err.message || err).slice(0, 200);
    loadTimeMs = Date.now() - navStart;
  } finally {
    await page.close();
  }

  return { status, msg, loadTimeMs };
}

async function main() {
  const { sites } = await relayGet('/active-sites');
  console.log(`[crawler] ${sites.length} site(s) actif(s) au total`);

  const browser = await chromium.launch();
  let totalPages = 0;
  let totalDown = 0;
  let totalSkipped = 0;

  try {
    for (const site of sites) {
      if (!site.kuma_group_id) {
        console.log(`[crawler] site "${site.client_name}" sans kuma_group_id, ignoré`);
        continue;
      }

      if (!isDue(site)) {
        console.log(
          `[crawler] "${site.client_name}" pas encore dû (interval ${site.crawl_interval_minutes ?? 1440}min, dernier crawl ${site.last_crawled_at}), skip`
        );
        totalSkipped += 1;
        continue;
      }

      let tokens;
      try {
        const data = await relayGet(`/push-tokens?groupId=${site.kuma_group_id}`);
        tokens = data.tokens;
      } catch (e) {
        console.error(`[crawler] impossible de récupérer les tokens pour "${site.client_name}":`, e.message);
        continue;
      }

      console.log(`[crawler] "${site.client_name}": ${tokens.length} page(s) à vérifier`);

      // Un seul context Playwright pour tout le groupe : on évite de relancer
      // un environnement de navigation par page alors qu'un groupe partage
      // le même site (95% des cas). Une page = un onglet dans ce context.
      const context = await browser.newContext();
      try {
        for (let i = 0; i < tokens.length; i++) {
          const token = tokens[i];
          totalPages += 1;
          const { status, msg, loadTimeMs } = await checkPage(context, token.url);
          if (status === 'down') totalDown += 1;
          console.log(`[crawler]   ${token.url} -> ${status} (${msg}) [${loadTimeMs != null ? Math.round(loadTimeMs) + 'ms' : '—'}]`);
          await pushStatus(token.pushToken, status, msg, loadTimeMs);

          // Petite pause entre les pages d'un même site pour éviter de
          // déclencher un rate-limiting (HTTP 429) côté serveur du client :
          // plusieurs pages du même domaine vérifiées trop vite peuvent
          // ressembler à du trafic abusif pour certaines protections.
          if (i < tokens.length - 1) {
            await new Promise((resolve) => setTimeout(resolve, 2000));
          }
        }
      } finally {
        await context.close();
      }

      try {
        await relayPost(`/sites/${site.id}/mark-crawled`);
      } catch (e) {
        console.error(`[crawler] échec mark-crawled pour "${site.client_name}":`, e.message);
      }
    }
  } finally {
    await browser.close();
  }

  console.log(
    `[crawler] terminé : ${totalPages} page(s) vérifiée(s), ${totalDown} en down, ${totalSkipped} site(s) skippé(s) (pas encore dû).`
  );
}

main().catch((err) => {
  console.error('[crawler] erreur fatale:', err);
  process.exit(1);
});
