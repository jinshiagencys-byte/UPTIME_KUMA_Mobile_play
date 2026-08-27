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

// Extrait le hostname d'une URL, ou null si invalide.
function getHostname(rawUrl) {
  try {
    return new URL(rawUrl).hostname;
  } catch {
    return null;
  }
}

// Une requête est considérée "first-party" (le backend du site qu'on teste)
// si elle vise le même hostname que la page, ou un sous-domaine de celui-ci
// (ex: api.markhorusbj.com pour markhorusbj.com). Tout le reste (Google
// Analytics, pixels pub, CDN tiers, polices, chat widgets...) est ignoré :
// ces appels sont fréquemment avortés par le navigateur sans que ce soit un
// signe de panne du site lui-même, et généraient des faux positifs (ex:
// beacon google-analytics.com/g/collect avorté = pas une panne du site).
function isFirstPartyRequest(requestUrl, pageHostname) {
  const reqHost = getHostname(requestUrl);
  if (!reqHost || !pageHostname) return false;
  return reqHost === pageHostname || reqHost.endsWith(`.${pageHostname}`);
}

// Vérifie une page : navigation OK + pas d'erreur API silencieuse en arrière-plan
// (c'est le but même du projet : détecter un backend qui répond en erreur
// alors que le frontend a l'air fonctionnel).
async function checkPage(context, url) {
  const page = await context.newPage();
  const pageHostname = getHostname(url);

  const apiErrors = [];
  const consoleErrors = [];
  // Requêtes first-party (xhr/fetch) encore en vol au moment où on regarde —
  // sert uniquement à produire un message clair si page.goto timeout parce
  // que ces appels restent bloqués en "pending" indéfiniment (le cas
  // "réalisations qui tournent en rond" observé manuellement : le navigateur
  // met bien plus de 20s à déclarer l'échec, donc notre propre timeout de
  // page.goto se déclenche avant que 'requestfailed' ait la moindre chance
  // de se déclencher).
  const pendingFirstPartyRequests = new Map();

  page.on('request', (req) => {
    const type = req.resourceType();
    if ((type === 'xhr' || type === 'fetch') && isFirstPartyRequest(req.url(), pageHostname)) {
      pendingFirstPartyRequests.set(req.url(), true);
    }
  });

  page.on('requestfinished', (req) => {
    pendingFirstPartyRequests.delete(req.url());
  });

  page.on('response', (response) => {
    const req = response.request();
    const type = req.resourceType();
    // On ne surveille que les appels XHR/fetch (appels API) vers le domaine
    // du site lui-même — pas les assets statiques, ni les appels vers des
    // domaines tiers (analytics, pubs, CDN) qui peuvent échouer sans rapport
    // avec la santé du site.
    if (
      (type === 'xhr' || type === 'fetch') &&
      isFirstPartyRequest(req.url(), pageHostname) &&
      response.status() >= 500
    ) {
      apiErrors.push(`${response.status()} ${req.url()}`);
    }
  });

  // Cas distinct du précédent : une requête peut ne JAMAIS recevoir de
  // réponse (connexion refusée, DNS, timeout, CORS bloqué côté navigateur,
  // certificat invalide...). Dans ce cas 'response' ne se déclenche pas du
  // tout — c'est le scénario "le backend n'arrive pas jusqu'au frontend" :
  // l'appel API part et disparaît silencieusement, sans jamais faire planter
  // le JS ni renvoyer un code d'erreur exploitable par 'response'. Kuma (et
  // l'ancienne version de ce script) ne voyaient pas ce cas du tout. Même
  // filtre first-party que ci-dessus : un beacon analytics avorté ne doit
  // pas faire passer le site en "down".
  page.on('requestfailed', (req) => {
    pendingFirstPartyRequests.delete(req.url());
    const type = req.resourceType();
    if ((type === 'xhr' || type === 'fetch') && isFirstPartyRequest(req.url(), pageHostname)) {
      const reason = req.failure()?.errorText || 'requête échouée';
      apiErrors.push(`${reason} ${req.url()}`);
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
    // Si page.goto a timeout ALORS QUE des appels API first-party sont
    // toujours en vol, on le dit explicitement plutôt que de renvoyer le
    // message générique Playwright ("Timeout 20000ms exceeded") — c'est le
    // cas concret observé manuellement (appels bloqués en "pending" dans le
    // Network tab, jamais résolus).
    if (pendingFirstPartyRequests.size > 0) {
      const stuckUrls = Array.from(pendingFirstPartyRequests.keys()).slice(0, 3).join(', ');
      msg = `Timeout: ${pendingFirstPartyRequests.size} appel(s) API du site sans réponse (ex: ${stuckUrls})`.slice(0, 200);
    } else {
      msg = String(err.message || err).slice(0, 200);
    }
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
