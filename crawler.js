const { chromium } = require('playwright');

const RELAY_URL = process.env.RELAY_URL;
const RELAY_SECRET = process.env.RELAY_SECRET;
const KUMA_URL = process.env.KUMA_URL;

// Domaines supplémentaires considérés comme "first-party" (backends externes).
const EXTRA_API_DOMAINS = (process.env.EXTRA_API_DOMAINS || '')
  .split(',')
  .map((d) => d.trim())
  .filter(Boolean);

// Durée d'observation après chargement (ms)
const POST_LOAD_WAIT_MS = parseInt(process.env.POST_LOAD_WAIT_MS || '60000', 10);

// Active le log de toutes les requêtes (debug)
const DEBUG_LOG_ALL_REQUESTS = (process.env.DEBUG_LOG_ALL_REQUESTS === 'true');

// Délai entre les requêtes (ms) pour ralentir le chargement et éviter le rate-limit.
const REQUEST_DELAY_MS = parseInt(process.env.REQUEST_DELAY_MS || '150', 10);

// Nombre de tentatives de rechargement en cas de 429 sur la page principale.
const MAX_429_RETRIES = parseInt(process.env.MAX_429_RETRIES || '3', 10);

// User-agent réaliste
const USER_AGENT =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';

if (!RELAY_URL || !RELAY_SECRET || !KUMA_URL) {
  console.error('RELAY_URL, RELAY_SECRET et KUMA_URL sont requis (env vars).');
  process.exit(1);
}

// ---------- Fonctions utilitaires (relayGet, relayPost, pushStatus, isDue, getHostname, etc.) ----------
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
    console.error('[push] pushToken manquant, envoi ignoré.');
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

function isDue(site) {
  if (!site.last_crawled_at) return true;
  const intervalMs = (site.crawl_interval_minutes ?? 1440) * 60 * 1000;
  const nextDue = new Date(site.last_crawled_at).getTime() + intervalMs;
  return Date.now() >= nextDue;
}

function getHostname(rawUrl) {
  try {
    return new URL(rawUrl).hostname;
  } catch {
    return null;
  }
}

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

// ---------- Vérification d'une page ----------
async function checkPage(context, url) {
  // Applique un délai entre les requêtes pour éviter les rafales
  await context.route('**/*', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, REQUEST_DELAY_MS));
    route.continue();
  });

  const page = await context.newPage();
  const pageHostname = getHostname(url);

  const apiErrors = [];
  const consoleErrors = [];
  const pendingFirstPartyRequests = new Map();

  // Écouteurs réseau (inchangés dans l'idée, mais on garde les logs)
  page.on('request', (req) => {
    const type = req.resourceType();
    if ((type === 'xhr' || type === 'fetch') && isFirstPartyRequest(req.url(), pageHostname)) {
      pendingFirstPartyRequests.set(req.url(), true);
      if (DEBUG_LOG_ALL_REQUESTS) {
        console.log(`[debug-req] Requête first-party: ${req.url()}`);
      }
    }
  });

  page.on('requestfinished', (req) => {
    pendingFirstPartyRequests.delete(req.url());
  });

  page.on('response', (response) => {
    const req = response.request();
    const type = req.resourceType();
    if ((type === 'xhr' || type === 'fetch') && isFirstPartyRequest(req.url(), pageHostname)) {
      if (response.status() >= 500) {
        apiErrors.push(`HTTP ${response.status()} ${req.url()}`);
      }
      if (DEBUG_LOG_ALL_REQUESTS) {
        console.log(`[debug-resp] Réponse ${response.status()} pour ${req.url()}`);
      }
    }
  });

  page.on('requestfailed', (req) => {
    pendingFirstPartyRequests.delete(req.url());
    const type = req.resourceType();
    if ((type === 'xhr' || type === 'fetch') && isFirstPartyRequest(req.url(), pageHostname)) {
      const reason = req.failure()?.errorText || 'requête échouée';
      apiErrors.push(`Réseau: ${reason} ${req.url()}`);
      if (DEBUG_LOG_ALL_REQUESTS) {
        console.log(`[debug-fail] Échec réseau (${reason}) sur ${req.url()}`);
      }
    }
  });

  page.on('pageerror', (err) => {
    consoleErrors.push(err.message);
  });

  let status = 'up';
  let msg = 'OK';
  let loadTimeMs = null;
  const navStart = Date.now();

  const PENDING_API_MAX_WAIT_MS = 4 * 60 * 1000;
  const PENDING_API_POLL_INTERVAL_MS = 500;

  async function waitForPendingRequestsToSettle(maxWaitMs) {
    const deadline = Date.now() + maxWaitMs;
    while (pendingFirstPartyRequests.size > 0 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, PENDING_API_POLL_INTERVAL_MS));
    }
    return pendingFirstPartyRequests.size === 0;
  }

  // Fonction pour recharger la page avec retry sur 429
  async function gotoWithRetry(page, url, maxRetries = MAX_429_RETRIES) {
    for (let attempt = 0; attempt < maxRetries; attempt++) {
      const response = await page.goto(url, { waitUntil: 'load', timeout: 20000 });
      if (response && response.status() !== 429) {
        return response;
      }
      if (response && response.status() === 429) {
        console.log(`[retry] 429 reçu, tentative ${attempt + 1}/${maxRetries}. Attente ${(attempt + 1) * 10}s...`);
        await page.waitForTimeout((attempt + 1) * 10000);
      }
    }
    return await page.goto(url, { waitUntil: 'load', timeout: 20000 }); // dernier essai
  }

  try {
    const response = await gotoWithRetry(page, url);

    if (!response || !response.ok()) {
      status = 'down';
      msg = `HTTP ${response ? response.status() : 'no response'}`;
    } else {
      console.log(`[debug-net] Observation post-chargement pendant ${POST_LOAD_WAIT_MS}ms...`);
      await new Promise((resolve) => setTimeout(resolve, POST_LOAD_WAIT_MS));

      if (pendingFirstPartyRequests.size > 0) {
        console.log(`[debug-net] ${pendingFirstPartyRequests.size} requête(s) first-party encore en cours. Attente...`);
      }
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

    // Mesure du temps de chargement
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

// ---------- Boucle principale ----------
async function main() {
  const { sites } = await relayGet('/active-sites');
  console.log(`[crawler] ${sites.length} site(s) actif(s) au total`);

  const browser = await chromium.launch({
    headless: true, // en production, headless "new" est mieux, mais on garde true
    args: [
      '--disable-blink-features=AutomationControlled',
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--disable-web-security',
      '--disable-features=IsolateOrigins,site-per-process',
    ],
  });

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
        console.log(`[crawler] "${site.client_name}" pas encore dû, skip`);
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

      const context = await browser.newContext({
        userAgent: USER_AGENT,
        viewport: { width: 1366, height: 768 },
        locale: 'fr-FR',
        timezoneId: 'Europe/Paris',
        colorScheme: 'light',
        deviceScaleFactor: 1,
        hasTouch: false,
        isMobile: false,
      });

      try {
        for (let i = 0; i < tokens.length; i++) {
          const token = tokens[i];
          totalPages += 1;
          const { status, msg, loadTimeMs } = await checkPage(context, token.url);
          if (status === 'down') totalDown += 1;
          console.log(`[crawler]   ${token.url} -> ${status} (${msg}) [${loadTimeMs != null ? Math.round(loadTimeMs) + 'ms' : '—'}]`);
          await pushStatus(token.pushToken, status, msg, loadTimeMs);

          // Pause plus longue entre les pages pour éviter le rate-limit
          if (i < tokens.length - 1) {
            await new Promise((resolve) => setTimeout(resolve, 10000)); // 10 secondes
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

  console.log(`[crawler] terminé : ${totalPages} page(s) vérifiée(s), ${totalDown} en down, ${totalSkipped} site(s) skippé(s).`);
}

main().catch((err) => {
  console.error('[crawler] erreur fatale:', err);
  process.exit(1);
});
