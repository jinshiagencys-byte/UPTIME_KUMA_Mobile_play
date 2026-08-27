const { chromium } = require('playwright');

const RELAY_URL = process.env.RELAY_URL;
const RELAY_SECRET = process.env.RELAY_SECRET;
const KUMA_URL = process.env.KUMA_URL;

// Domaines supplémentaires (backends externes)
const EXTRA_API_DOMAINS = (process.env.EXTRA_API_DOMAINS || '')
  .split(',')
  .map((d) => d.trim())
  .filter(Boolean);

// Sélecteur CSS pour valider que le frontend a bien reçu les données du backend
const BACKEND_DATA_SELECTOR = process.env.BACKEND_DATA_SELECTOR || '.data-item';

// Détection de rate-limit sur les ressources first-party
const DETECT_429_ON_ASSETS = (process.env.DETECT_429_ON_ASSETS === 'true');
const MAX_429_ASSETS = parseInt(process.env.MAX_429_ASSETS || '5', 10);

// Active le log de TOUTES les requêtes pour debug
const DEBUG_LOG_ALL_REQUESTS = (process.env.DEBUG_LOG_ALL_REQUESTS === 'true');

// Timeouts de sécurité
const NAVIGATION_TIMEOUT = 20000;
const NETWORK_IDLE_TIMEOUT = 10000;
const UI_DATA_TIMEOUT = 8000;

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
  let count429Assets = 0;

  // Écouteur des réponses réseau
  const onResponse = (response) => {
    const req = response.request();
    if (isRelevantDomain(req.url(), pageHostname)) {
      const type = req.resourceType();
      const status = response.status();
      
      if (DEBUG_LOG_ALL_REQUESTS) {
        console.log(`[debug-resp] Réponse ${status} pour ${type} ${req.url()}`);
      }

      // Détection de rate-limit sur les assets
      if (status === 429 && DETECT_429_ON_ASSETS) {
        if (['script', 'stylesheet', 'image', 'font'].includes(type)) {
          count429Assets++;
        }
      }

      // Capture des erreurs d'API pures (XHR/Fetch) en échec HTTP (>=500)
      if ((type === 'xhr' || type === 'fetch') && status >= 500) {
        apiErrors.push(`HTTP ${status} sur ${req.url()}`);
      }
    }
  };

  // Écouteur des pannes réseau
  const onRequestFailed = (req) => {
    if (isRelevantDomain(req.url(), pageHostname)) {
      const type = req.resourceType();
      if (type === 'xhr' || type === 'fetch') {
        const reason = req.failure()?.errorText || 'requête échouée';
        if (DEBUG_LOG_ALL_REQUESTS) {
          console.log(`[debug-fail] Échec réseau (${reason}) pour ${type} ${req.url()}`);
        }
        apiErrors.push(`Réseau: ${reason} sur ${req.url()}`);
      }
    }
  };

  page.on('response', onResponse);
  page.on('requestfailed', onRequestFailed);

  let status = 'up';
  let msg = 'OK';
  const navStart = Date.now();

  try {
    // 1. Navigation initiale
    const response = await page.goto(url, { waitUntil: 'commit', timeout: NAVIGATION_TIMEOUT });

    if (!response || !response.ok()) {
      status = 'down';
      msg = `HTTP ${response ? response.status() : 'no response'}`;
    } else {
      // 2. Attente intelligente de la fin du trafic réseau
      await page.waitForLoadState('networkidle', { timeout: NETWORK_IDLE_TIMEOUT }).catch(() => {
        if (DEBUG_LOG_ALL_REQUESTS) {
          console.log('[debug-net] Le trafic réseau sature ou ne s\'est pas calmé.');
        }
      });

      // 3. Validation Web-First : On force l'attente du rendu de la donnée
      await page.locator(BACKEND_DATA_SELECTOR).waitFor({ state: 'visible', timeout: UI_DATA_TIMEOUT })
        .catch(() => {
          apiErrors.push(`Données UI absentes (Attente sélecteur: ${BACKEND_DATA_SELECTOR})`);
        });

      // 4. Évaluation finale des erreurs accumulées
      if (DETECT_429_ON_ASSETS && count429Assets >= MAX_429_ASSETS) {
        status = 'down';
        msg = `Rate-limit détecté (${count429Assets} ressources en 429)`.slice(0, 200);
      } else if (apiErrors.length > 0) {
        status = 'down';
        msg = `Backend Down: ${apiErrors[0]}`.slice(0, 200);
      }
    }
  } catch (error) {
    status = 'down';
    msg = `Erreur Script: ${error.message}`.slice(0, 200);
  } finally {
    const loadTimeMs = Date.now() - navStart;

    // Nettoyage pour empêcher les fuites de mémoire
    page.off('response', onResponse);
    page.off('requestfailed', onRequestFailed);
    await page.close();

    return { status, msg, loadTimeMs };
  }
}

// ==================== ORCHESTRATEUR PRINCIPAL ====================

(async () => {
  console.log('Démarrage du worker de monitoring...');
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();

  try {
    // Récupération des sites depuis votre serveur relais
    const { sites } = await relayGet('/api/sites');
    console.log(`${sites.length} site(s) configuré(s) au total.`);

    for (const site of sites) {
      if (!isDue(site)) {
        continue; 
      }

      console.log(`Vérification requise pour : ${site.url}`);
      
      // Notification de début de crawl au relais
      await relayPost(`/api/sites/${site.id}/crawl-start`);

      // Exécution du test de la page
      const result = await checkPage(context, site.url);
      
      console.log(`[${site.url}] Statut final : ${result.status} - ${result.msg}`);

      // Push du résultat vers Uptime Kuma
      if (site.kuma_push_token) {
        await pushStatus(site.kuma_push_token, result.status, result.msg, result.loadTimeMs);
      }

      // Notification de fin de crawl avec le statut
      await relayPost(`/api/sites/${site.id}/crawl-end`, {
        status: result.status,
        msg: result.msg,
      });
    }
  } catch (err) {
    console.error('Erreur critique dans la boucle principale :', err.message);
  } finally {
    await context.close();
    await browser.close();
    console.log('Worker terminé, navigateur fermé.');
  }
})();
