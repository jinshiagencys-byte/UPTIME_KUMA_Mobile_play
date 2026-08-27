const { chromium } = require('playwright');

const RELAY_URL = process.env.RELAY_URL;
const RELAY_SECRET = process.env.RELAY_SECRET;
const KUMA_URL = process.env.KUMA_URL;

// Domaines supplémentaires (backends externes) – pas nécessaire pour markhorus
const EXTRA_API_DOMAINS = (process.env.EXTRA_API_DOMAINS || '')
  .split(',')
  .map((d) => d.trim())
  .filter(Boolean);

// Durée d'observation après chargement (ms)
const POST_LOAD_WAIT_MS = parseInt(process.env.POST_LOAD_WAIT_MS || '120000', 10);

// Active le log de TOUTES les requêtes pour debug
const DEBUG_LOG_ALL_REQUESTS = (process.env.DEBUG_LOG_ALL_REQUESTS === 'true');

// Détection de rate-limit sur les ressources first-party
const DETECT_429_ON_ASSETS = (process.env.DETECT_429_ON_ASSETS === 'true');
const MAX_429_ASSETS = parseInt(process.env.MAX_429_ASSETS || '5', 10);

if (!RELAY_URL || !RELAY_SECRET || !KUMA_URL) {
  console.error('RELAY_URL, RELAY_SECRET et KUMA_URL sont requis (env vars).');
  process.exit(1);
}

// ==================== FONCTIONS UTILITAIRES ====================

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

// Domaine pertinent = first-party ou domaine contenant "markhorus" si debug
function isRelevantDomain(requestUrl, pageHostname) {
  const reqHost = getHostname(requestUrl);
  if (!reqHost || !pageHostname) return false;
  if (reqHost === pageHostname || reqHost.endsWith(`.${pageHostname}`)) return true;
  if (EXTRA_API_DOMAINS.some((d) => reqHost === d || reqHost.endsWith(`.${d}`))) return true;
  if (DEBUG_LOG_ALL_REQUESTS && reqHost.includes('markhorus')) return true;
  return false;
}

// ==================== VÉRIFICATION D'UNE PAGE ====================

async function checkPage(context, url) {
  const page = await context.newPage();
  const pageHostname = getHostname(url);

  const apiErrors = [];
  const consoleErrors = [];
  const pendingRelevantRequests = new Map();
  let count429Assets = 0;

  // ----- Écouteurs réseau -----
  page.on('request', (req) => {
    const type = req.resourceType();
    if (isRelevantDomain(req.url(), pageHostname)) {
      pendingRelevantRequests.set(req.url(), { type, start: Date.now() });
      if (DEBUG_LOG_ALL_REQUESTS) {
        console.log(`[debug-req] Requête ${type}: ${req.url()}`);
      }
    }
  });

  page.on('requestfinished', (req) => {
    pendingRelevantRequests.delete(req.url());
  });

  page.on('response', (response) => {
    const req = response.request();
    if (isRelevantDomain(req.url(), pageHostname)) {
      const status = response.status();
      if (DEBUG_LOG_ALL_REQUESTS) {
        console.log(`[debug-resp] Réponse ${status} pour ${req.resourceType()} ${req.url()}`);
      }
      // Détection de rate-limit sur les assets (scripts, styles, images, fonts)
      if (status === 429 && DETECT_429_ON_ASSETS) {
        const type = req.resourceType();
        if (['script', 'stylesheet', 'image', 'font'].includes(type)) {
          count429Assets++;
        }
      }
      if (status >= 500) {
        apiErrors.push(`HTTP ${status} ${req.url()}`);
      }
    }
  });

  page.on('requestfailed', (req) => {
    pendingRelevantRequests.delete(req.url());
    if (isRelevantDomain(req.url(), pageHostname)) {
      const reason = req.failure()?.errorText || 'requête échouée';
      if (DEBUG_LOG_ALL_REQUESTS) {
        console.log(`[debug-fail] Échec réseau (${reason}) pour ${req.resourceType()} ${req.url()}`);
      }
      // On ne remonte que les échecs XHR/Fetch comme erreurs API
      if (req.resourceType() === 'xhr' || req.resourceType() === 'fetch') {
        apiErrors.push(`Réseau: ${reason} ${req.url()}`);
      }
    }
  });

  // WebSocket (optionnel)
  page.on('websocket', (ws) => {
    const wsUrl = ws.url();
    if (isRelevantDomain(wsUrl, pageHostname)) {
      console.log(`[debug-ws] WebSocket ouvert: ${wsUrl}`);
      ws.on('framereceived', (frame) => {
        console.log(`[debug-ws] Frame reçu (${frame.payload.length} octets) sur ${wsUrl}`);
      });
      ws.on('close', () => {
        console.log(`[debug-ws] WebSocket fermé: ${wsUrl}`);
      });
      ws.on('sockererror', (err) => {
        console.log(`[debug-ws] Erreur WebSocket sur ${wsUrl}: ${err.message}`);
        apiErrors.push(`WebSocket erreur: ${err.message} ${wsUrl}`);
      });
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
    while (pendingRelevantRequests.size > 0 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, PENDING_API_POLL_INTERVAL_MS));
    }
    return pendingRelevantRequests.size === 0;
  }

  try {
    const response = await page.goto(url, { waitUntil: 'load', timeout: 20000 });

    if (!response || !response.ok()) {
      status = 'down';
      msg = `HTTP ${response ? response.status() : 'no response'}`;
    } else {
      console.log(`[debug-net] Observation post-chargement pendant ${POST_LOAD_WAIT_MS}ms...`);
      await new Promise((resolve) => setTimeout(resolve, POST_LOAD_WAIT_MS));

      if (pendingRelevantRequests.size > 0) {
        console.log(`[debug-net] ${pendingRelevantRequests.size} requête(s) pertinente(s) encore en cours. Attente...`);
      }
      const settled = await waitForPendingRequestsToSettle(PENDING_API_MAX_WAIT_MS);

      // Détermination du statut final
      if (DETECT_429_ON_ASSETS && count429Assets >= MAX_429_ASSETS) {
        status = 'down';
        msg = `Rate-limit détecté (${count429Assets} ressources en 429)`.slice(0, 200);
      } else if (apiErrors.length > 0) {
        status = 'down';
        msg = `API Down (${apiErrors[0]})`.slice(0, 200);
      } else if (consoleErrors.length > 0) {
        status = 'down';
        msg = `JS Error: ${consoleErrors[0]}`.slice(0, 200);
      } else if (!settled) {
        status = 'down';
        const stuckUrls = Array.from(pendingRelevantRequests.keys())
          .slice(0, 2)
          .map((u) => u.replace(/^https?:\/\//, '').slice(0, 40))
          .join(', ');
        msg = `Backend muet (Timeout 4min): ${pendingRelevantRequests.size} requête(s) sans réponse (ex: ${stuckUrls})`.slice(0, 200);
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

// ==================== BOUCLE PRINCIPALE ====================

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
        console.log(`[crawler] "${site.client_name}" pas encore dû (interval ${site.crawl_interval_minutes ?? 1440}min, dernier crawl ${site.last_crawled_at}), skip`);
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

          // Pause entre pages d'un même site (augmentable)
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

  console.log(`[crawler] terminé : ${totalPages} page(s) vérifiée(s), ${totalDown} en down, ${totalSkipped} site(s) skippé(s).`);
}

main().catch((err) => {
  console.error('[crawler] erreur fatale:', err);
  process.exit(1);
});
