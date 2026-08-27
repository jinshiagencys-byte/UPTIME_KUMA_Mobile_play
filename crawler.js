const { chromium } = require('playwright');

const RELAY_URL = process.env.RELAY_URL;
const RELAY_SECRET = process.env.RELAY_SECRET;
const KUMA_URL = process.env.KUMA_URL;

// Domaines supplémentaires (backends externes)
const EXTRA_API_DOMAINS = (process.env.EXTRA_API_DOMAINS || '')
  .split(',')
  .map((d) => d.trim())
  .filter(Boolean);

// Sélecteur CSS d'un élément généré par le backend (Ex: une ligne de tableau, une carte, un item)
// REMPLACEZ '.data-item' par un vrai sélecteur de votre application pour une efficacité maximale
const BACKEND_DATA_SELECTOR = process.env.BACKEND_DATA_SELECTOR || '.data-item';

// Configuration des timeouts (en ms)
const NAVIGATION_TIMEOUT = 15000;
const NETWORK_IDLE_TIMEOUT = 10000;
const UI_DATA_TIMEOUT = 8000;

// Active le log de TOUTES les requêtes pour debug
const DEBUG_LOG_ALL_REQUESTS = (process.env.DEBUG_LOG_ALL_REQUESTS === 'true');

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
  const criticalApiErrors = [];

  // Écouteur des réponses réseau (Capture des erreurs API/Backend)
  const onResponse = (response) => {
    const req = response.request();
    if (isRelevantDomain(req.url(), pageHostname)) {
      const type = req.resourceType();
      const status = response.status();
      
      if (DEBUG_LOG_ALL_REQUESTS) {
        console.log(`[debug-resp] Réponse ${status} pour ${type} ${req.url()}`);
      }

      // Capture uniquement les erreurs de données (XHR/Fetch) en échec HTTP (>=500)
      if ((type === 'xhr' || type === 'fetch') && status >= 500) {
        criticalApiErrors.push(`HTTP ${status} sur ${req.url()}`);
      }
    }
  };

  // Écouteur des pannes réseau pures (Timeout, DNS, Reset de connexion)
  const onRequestFailed = (req) => {
    if (isRelevantDomain(req.url(), pageHostname)) {
      const type = req.resourceType();
      if (type === 'xhr' || type === 'fetch') {
        const reason = req.failure()?.errorText || 'échec réseau';
        if (DEBUG_LOG_ALL_REQUESTS) {
          console.log(`[debug-fail] Échec réseau (${reason}) pour ${type} ${req.url()}`);
        }
        criticalApiErrors.push(`Réseau: ${reason} sur ${req.url()}`);
      }
    }
  };

  page.on('response', onResponse);
  page.on('requestfailed', onRequestFailed);

  let status = 'up';
  let msg = 'OK';
  const navStart = Date.now();

  try {
    // 1. Navigation initiale rapide
    const response = await page.goto(url, { waitUntil: 'commit', timeout: NAVIGATION_TIMEOUT });

    if (!response || !response.ok()) {
      status = 'down';
      msg = `HTTP ${response ? response.status() : 'no response'}`;
    } else {
      // 2. Attente intelligente que le trafic réseau se calme
      await page.waitForLoadState('networkidle', { timeout: NETWORK_IDLE_TIMEOUT }).catch(() => {
        if (DEBUG_LOG_ALL_REQUESTS) {
          console.log('[debug-net] Le trafic réseau ne s\'est pas arrêté dans le délai imparti.');
        }
      });

      // 3. Validation visuelle des données du backend
      await page.locator(BACKEND_DATA_SELECTOR).waitFor({ state: 'visible', timeout: UI_DATA_TIMEOUT })
        .catch(() => {
          criticalApiErrors.push(`Données UI absentes (Sélecteur: ${BACKEND_DATA_SELECTOR})`);
        });

      // 4. Analyse des logs d'erreurs accumulés
      if (criticalApiErrors.length > 0) {
        status = 'down';
        msg = `Backend Down: ${criticalApiErrors[0]}`.slice(0, 200);
      }
    }
  } catch (error) {
    status = 'down';
    msg = `Erreur Script: ${error.message}`.slice(0, 200);
  } finally {
    const loadTimeMs = Date.now() - navStart;
    
    // Nettoyage strict des écouteurs pour éviter les fuites de mémoire
    page.off('response', onResponse);
    page.off('requestfailed', onRequestFailed);
    await page.close();

    return { status, msg, loadTimeMs };
  }
}

// ==================== SCRIPT PRINCIPAL / EXÉCUTION ====================

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();

  try {
    // Exemple d'exécution sur une URL cible passée en variable ou en paramètre
    const targetUrl = process.env.TARGET_URL || 'https://example.com';
    const pushToken = process.env.KUMA_PUSH_TOKEN;

    console.log(`Démarrage de la vérification pour : ${targetUrl}`);
    const result = await checkPage(context, targetUrl);
    
    console.log(`[Résultat] Statut: ${result.status} | Message: ${result.msg} (${result.loadTimeMs}ms)`);
    
    if (pushToken) {
      await pushStatus(pushToken, result.status, result.msg, result.loadTimeMs);
    }
  } catch (err) {
    console.error('Erreur globale d\'exécution:', err.message);
  } finally {
    await context.close();
    await browser.close();
  }
})();
