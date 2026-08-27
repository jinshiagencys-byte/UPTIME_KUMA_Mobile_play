const { chromium } = require('playwright');

const RELAY_URL = process.env.RELAY_URL;
const RELAY_SECRET = process.env.RELAY_SECRET;
const KUMA_URL = process.env.KUMA_URL;
// Domaines supplémentaires considérés comme "first-party" (backends externes).
// Séparez plusieurs domaines par des virgules, ex: "api.monsite.com,backend.autre.com"
const EXTRA_API_DOMAINS = (process.env.EXTRA_API_DOMAINS || '')
  .split(',')
  .map((d) => d.trim())
  .filter(Boolean);

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

// Une requête est considérée "first-party" si elle vise :
// - le même hostname que la page,
// - un sous-domaine de celui-ci (ex: api.markhorusbj.com pour markhorusbj.com),
// - un des domaines listés dans EXTRA_API_DOMAINS (backends externes connus).
function isFirstPartyRequest(requestUrl, pageHostname) {
  const reqHost = getHostname(requestUrl);
  if (!reqHost || !pageHostname) return false;
  if (reqHost === pageHostname || reqHost.endsWith(`.${pageHostname}`)) {
    return true;
  }
  return EXTRA_API_DOMAINS.some(
    (domain) => reqHost === domain || reqHost.endsWith(`.${domain}`)
  );
}

// Vérifie une page : navigation OK + pas d'erreur API silencieuse en arrière-plan
// (c'est le but même du projet : détecter un backend qui répond en erreur
// alors que le frontend a l'air fonctionnel).
async function checkPage(context, url) {
  const page = await context.newPage();
  const pageHostname = getHostname(url);

  const apiErrors = [];
  const consoleErrors = [];
  // Requêtes first-party (xhr/fetch) encore en vol au moment où on regarde.
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
    if (
      (type === 'xhr' || type === 'fetch') &&
      isFirstPartyRequest(req.url(), pageHostname) &&
      response.status() >= 500
    ) {
      apiErrors.push(`HTTP ${response.status()} ${req.url()}`);
    }
  });

  page.on('requestfailed', (req) => {
    pendingFirstPartyRequests.delete(req.url());
    const type = req.resourceType();
    if ((type === 'xhr' || type === 'fetch') && isFirstPartyRequest(req.url(), pageHostname)) {
      const reason = req.failure()?.errorText || 'requête échouée';
      apiErrors.push(`Réseau: ${reason} ${req.url()}`);
    }
  });

  page.on('pageerror', (err) => {
    consoleErrors.push(err.message);
  });

  let status = 'up';
  let msg = 'OK';
  let loadTimeMs = null;
  const navStart = Date.now();

  const PENDING_API_MAX_WAIT_MS = 4 * 60 * 1000; // 4 minutes
  const PENDING_API_POLL_INTERVAL_MS = 500;

  async function waitForPendingRequestsToSettle(maxWaitMs) {
    const deadline = Date.now() + maxWaitMs;
    while (pendingFirstPartyRequests.size > 0 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, PENDING_API_POLL_INTERVAL_MS));
    }
    return pendingFirstPartyRequests.size === 0;
  }

  try {
    // Chargement de la page : on attend 'load' pour être sûr que les scripts
    // de la SPA sont bien chargés et que les appels API sont probablement lancés.
    const response = await page.goto(url, { waitUntil: 'load', timeout: 20000 });

    if (!response || !response.ok()) {
      status = 'down';
      msg = `HTTP ${response ? response.status() : 'no response'}`;
    } else {
      // Attente du réseau "idle" pendant 10 secondes maximum pour laisser
      // aux frameworks JS le temps de lancer leurs appels API.
      // Si des requêtes restent pendantes, ce timeout expirera et on passera
      // à l'étape suivante (les requêtes sont déjà dans pendingFirstPartyRequests).
      try {
        await page.waitForLoadState('networkidle', { timeout: 10000 });
      } catch (e) {
        // Timeout normal si des requêtes API sont en cours ou bloquées.
      }

      // Attente CONFIRMÉE des appels API en cours (jusqu'à 4 minutes).
      const settled = await waitForPendingRequestsToSettle(PENDING_API_MAX_WAIT_MS);

      if (apiErrors.length > 0) {
        status = 'down';
        msg = `API Down (${apiErrors[0]})`.slice(0, 200);
      } else if (consoleErrors.length > 0) {
        status = 'down';
        msg = `JS Error: ${consoleErrors[0]}`.slice(0, 200);
      } else if (!settled) {
        status = 'down';
        const stuckUrls = Array.from(pendingFirstPartyRequests.keys())
          .slice(0, 2)
          .map((u) => u.replace(/^https?:\/\//, '').slice(0, 40))
          .join(', ');
        msg = `Backend muet (Timeout 4min): ${pendingFirstPartyRequests.size} requête(s) API sans réponse (ex: ${stuckUrls})`.slice(0, 200);
      }
    }

    // Mesure du temps de chargement réel (Navigation Timing API).
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

      const context = await browser.newContext();
      try {
        for (let i = 0; i < tokens.length; i++) {
          const token = tokens[i];
          totalPages += 1;
          const { status, msg, loadTimeMs } = await checkPage(context, token.url);
          if (status === 'down') totalDown += 1;
          console.log(`[crawler]   ${token.url} -> ${status} (${msg}) [${loadTimeMs != null ? Math.round(loadTimeMs) + 'ms' : '—'}]`);
          await pushStatus(token.pushToken, status, msg, loadTimeMs);

          // Petite pause entre les pages d'un même site pour éviter le rate-limiting.
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
