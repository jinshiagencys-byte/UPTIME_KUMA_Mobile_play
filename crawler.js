const { chromium } = require('playwright');

// Variables d'environnement
const RELAY_URL = process.env.RELAY_URL;
const RELAY_SECRET = process.env.RELAY_SECRET;
const KUMA_URL = process.env.KUMA_URL;

// Paramètres de configuration (avec valeurs par défaut)
const NETWORK_IDLE_TIMEOUT_MS = parseInt(process.env.NETWORK_IDLE_TIMEOUT_MS || '15000', 10);
const API_WAIT_TIMEOUT_MS = parseInt(process.env.API_WAIT_TIMEOUT_MS || '30000', 10);
const POST_LOAD_WAIT_MS = parseInt(process.env.POST_LOAD_WAIT_MS || '30000', 10);
const MIN_TEXT_LENGTH = parseInt(process.env.MIN_TEXT_LENGTH || '100', 10);
const IGNORED_DOMAINS = (process.env.IGNORED_DOMAINS || 'google-analytics.com,doubleclick.net,facebook.net,cdn.jsdelivr.net')
  .split(',')
  .map(d => d.trim());

console.log('[crawler] Début du script');

// --- Utilitaires ---

function getHostname(url) {
  try {
    return new URL(url).hostname;
  } catch {
    return '';
  }
}

function isIgnoredDomain(url) {
  const hostname = getHostname(url);
  return IGNORED_DOMAINS.some(domain => hostname.includes(domain));
}

function checkIfDataPresent(json) {
  if (!json) return false;
  if (Array.isArray(json) && json.length > 0) return true;
  if (json.data && Array.isArray(json.data) && json.data.length > 0) return true;
  if (json.items && Array.isArray(json.items) && json.items.length > 0) return true;
  if (json.results && Array.isArray(json.results) && json.results.length > 0) return true;
  if (json.products && Array.isArray(json.products) && json.products.length > 0) return true;
  if (typeof json === 'object' && !Array.isArray(json)) {
    const keys = Object.keys(json);
    if (keys.length > 0 && !json.error && !json.message) return true;
  }
  return false;
}

function countDataItems(json) {
  if (!json) return 0;
  if (Array.isArray(json)) return json.length;
  if (json.data && Array.isArray(json.data)) return json.data.length;
  if (json.items && Array.isArray(json.items)) return json.items.length;
  if (json.results && Array.isArray(json.results)) return json.results.length;
  if (json.products && Array.isArray(json.products)) return json.products.length;
  return 0;
}

// --- Appels HTTP vers le relay (via fetch) ---

async function relayGet(path) {
  const url = `${RELAY_URL}${path}`;
  const res = await fetch(url, {
    headers: { 'x-relay-secret': RELAY_SECRET }
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status} sur ${url}`);
  }
  return await res.json();
}

async function relayPost(path, body = {}) {
  const url = `${RELAY_URL}${path}`;
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'x-relay-secret': RELAY_SECRET,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify(body)
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status} sur ${url}`);
  }
  return await res.json();
}

// --- Récupération des pages (push tokens) d'un groupe ---

async function getPagesForSite(groupId) {
  const data = await relayGet(`/push-tokens?groupId=${groupId}`);
  if (!data.success) {
    throw new Error('Erreur lors de la récupération des pages');
  }
  return data.tokens; // [{ url, monitorId, pushToken, name }]
}

// --- Déterminer si un site est dû ---

function isDue(site) {
  const last = site.last_crawled_at ? new Date(site.last_crawled_at) : null;
  const intervalMinutes = site.crawl_interval_minutes || 1440; // 24h par défaut
  const now = new Date();
  const due = !last || (now - last) / 60000 >= intervalMinutes;
  console.log(`[isDue] site ${site.id} (${site.client_name}) : last=${last}, interval=${intervalMinutes}min, due=${due}`);
  return due;
}

// --- Marquer un site comme crawlé ---

async function markSiteCrawled(siteId) {
  try {
    await relayPost(`/sites/${siteId}/mark-crawled`, {});
    console.log(`[crawler] Site ${siteId} marqué comme crawlé.`);
  } catch (err) {
    console.error(`[crawler] Erreur lors du marquage du site ${siteId} :`, err.message);
  }
}

// --- Envoyer le statut à Kuma via push token ---

async function sendStatusToKuma(pushToken, status, msg) {
  const kumaPushUrl = `${KUMA_URL}/api/push/${pushToken}`;
  const params = new URLSearchParams();
  params.append('status', status === 'up' ? 'up' : 'down');
  if (msg) params.append('msg', msg);
  const url = `${kumaPushUrl}?${params.toString()}`;
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`HTTP ${res.status} lors du push vers Kuma`);
  }
}

// --- Vérification d'une page (version améliorée) ---

async function checkPage(context, url) {
  const page = await context.newPage();
  const navStart = Date.now();
  let status = 'up';
  let msg = 'OK';
  let loadTimeMs = null;

  // Collecte des requêtes XHR/fetch
  const apiRequests = [];
  const failedRequests = [];
  let apiDataReceived = false;
  let apiDataHasContent = false;
  let apiDataCount = 0;
  let uiContent = { textLength: 0, elementCount: 0, hasContent: false };

  // Intercepter les requêtes
  page.on('request', (request) => {
    if (request.resourceType() === 'xhr' || request.resourceType() === 'fetch') {
      const reqUrl = request.url();
      if (!isIgnoredDomain(reqUrl)) {
        apiRequests.push({
          url: reqUrl,
          method: request.method(),
          startTime: Date.now()
        });
      }
    }
  });

  // Intercepter les réponses (pour analyser le contenu JSON)
  page.on('response', async (response) => {
    const req = response.request();
    if (req.resourceType() === 'xhr' || req.resourceType() === 'fetch') {
      const url = response.url();
      if (!isIgnoredDomain(url)) {
        const statusCode = response.status();
        if (statusCode >= 500) {
          failedRequests.push({ url, status: statusCode, type: 'http_error' });
          return;
        }

        // Analyser les réponses JSON des endpoints API
        if (statusCode === 200 && (url.includes('/api/') || url.includes('/graphql'))) {
          try {
            const json = await response.json().catch(() => null);
            if (json) {
              apiDataReceived = true;
              const hasData = checkIfDataPresent(json);
              if (hasData) {
                apiDataHasContent = true;
                apiDataCount += countDataItems(json);
              } else {
                // API a répondu 200 mais sans données
                failedRequests.push({
                  url,
                  status: 200,
                  type: 'empty_data',
                  message: 'API a répondu 200 mais données vides'
                });
              }
            }
          } catch (e) {
            // Pas du JSON, on ignore
          }
        }
      }
    }
  });

  // Détecter les échecs réseau
  page.on('requestfailed', (request) => {
    if (request.resourceType() === 'xhr' || request.resourceType() === 'fetch') {
      if (!isIgnoredDomain(request.url())) {
        failedRequests.push({
          url: request.url(),
          type: 'network_error',
          error: request.failure()?.errorText || 'failed'
        });
      }
    }
  });

  try {
    // Charger la page
    const response = await page.goto(url, {
      waitUntil: 'load',
      timeout: 30000
    });

    if (!response || !response.ok()) {
      status = 'down';
      msg = `HTTP ${response ? response.status() : 'no response'}`;
    } else {
      // Attendre que les requêtes API se terminent (timeout configurable)
      console.log(`[crawler] Attente de ${API_WAIT_TIMEOUT_MS}ms pour les requêtes API...`);
      await page.waitForTimeout(API_WAIT_TIMEOUT_MS);

      // Vérifier le contenu affiché dans le DOM
      uiContent = await page.evaluate(() => {
        const textContent = document.body.innerText || '';
        const dataElements = document.querySelectorAll('[class*="item"], [class*="product"], [class*="card"], li, .post, .article');
        return {
          textLength: textContent.length,
          elementCount: dataElements.length,
          hasContent: textContent.length > 200 && dataElements.length > 0
        };
      });

      // Décision finale
      const errors = [];

      if (apiRequests.length === 0) {
        errors.push('Aucun appel API détecté (site statique ou backend muet)');
      }

      if (failedRequests.length > 0) {
        const errorMessages = failedRequests.map(r =>
          r.type === 'empty_data' ? `${r.url} (données vides)` :
          r.type === 'http_error' ? `${r.url} (HTTP ${r.status})` :
          `${r.url} (${r.error || 'échec réseau'})`
        );
        errors.push(`Échecs détectés : ${errorMessages.join(', ')}`);
      }

      if (!uiContent.hasContent) {
        errors.push('Page ne semble pas afficher de données (texte court ou absence d\'éléments)');
      }

      if (apiDataReceived && apiDataHasContent && apiDataCount > 0) {
        const uiElementCount = uiContent.elementCount;
        if (uiElementCount < apiDataCount * 0.5) {
          errors.push(`Incohérence : API retourne ${apiDataCount} éléments, mais seulement ${uiElementCount} affichés`);
        }
      }

      if (errors.length > 0) {
        status = 'down';
        msg = errors.join('; ');
      } else {
        status = 'up';
        msg = 'OK (API et contenu validés)';
      }
    }

    loadTimeMs = Date.now() - navStart;
  } catch (err) {
    status = 'down';
    msg = String(err.message || err).slice(0, 200);
    loadTimeMs = Date.now() - navStart;
  } finally {
    // Logs détaillés
    const observedDomains = new Set();
    apiRequests.forEach(r => {
      try { observedDomains.add(new URL(r.url).hostname); } catch {}
    });
    if (observedDomains.size > 0) {
      console.log(`[crawler] Domaines XHR/fetch observés pour ${url} :`, Array.from(observedDomains).join(', '));
      console.log(`[crawler] URLs complètes (échantillon) :`, apiRequests.slice(0, 5).map(r => r.url).join(', '));
    } else {
      console.log(`[crawler] Aucune requête XHR/fetch observée pour ${url}`);
    }
    if (failedRequests.length > 0) {
      console.log(`[crawler] Échecs détectés :`, failedRequests.map(r =>
        `${r.url} (${r.type})`
      ).join(', '));
    }
    console.log(`[crawler] UI : ${apiDataCount} données API, ${uiContent.elementCount} éléments affichés`);
    console.log(`[crawler] Résultat final : ${status} - ${msg}`);

    await page.close();
  }

  return { status, msg, loadTimeMs };
}

// --- Fonction principale ---

async function main() {
  console.log('[crawler] main() démarrée');

  try {
    // 1. Récupérer les sites actifs depuis le relay
    console.log('[crawler] Appel à /active-sites');
    const sitesData = await relayGet('/active-sites');
    console.log('[crawler] Réponse /active-sites reçue, sites trouvés :', sitesData.sites ? sitesData.sites.length : 0);

    if (!sitesData.success || !sitesData.sites || sitesData.sites.length === 0) {
      console.log('[crawler] Aucun site actif trouvé. Fin du crawler.');
      return;
    }

    // 2. Filtrer les sites dus
    const dueSites = sitesData.sites.filter(site => isDue(site));
    console.log(`[crawler] ${dueSites.length} site(s) dus sur ${sitesData.sites.length} au total.`);

    if (dueSites.length === 0) {
      console.log('[crawler] Aucun site à crawler pour le moment. Fin.');
      return;
    }

    // 3. Traiter chaque site
    for (const site of dueSites) {
      console.log(`[crawler] Traitement du site ${site.id} : ${site.client_name} (${site.site_url})`);

      // Récupérer les pages (push tokens) associées au groupe
      let pages = [];
      try {
        pages = await getPagesForSite(site.kuma_group_id);
        console.log(`[crawler] ${pages.length} pages trouvées pour le groupe ${site.kuma_group_id}`);
      } catch (err) {
        console.error(`[crawler] Erreur lors de la récupération des pages pour le site ${site.id}:`, err.message);
        await markSiteCrawled(site.id);
        continue;
      }

      if (pages.length === 0) {
        console.log(`[crawler] Aucune page à vérifier pour le site ${site.id}.`);
        await markSiteCrawled(site.id);
        continue;
      }

      // Créer un contexte Playwright partagé pour ce site
      const browser = await chromium.launch({ headless: true });
      const context = await browser.newContext({
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
      });

      for (const pageInfo of pages) {
        console.log(`[crawler] Vérification de la page : ${pageInfo.url}`);
        const result = await checkPage(context, pageInfo.url);
        console.log(`[crawler] Résultat pour ${pageInfo.url} : status=${result.status}, msg=${result.msg}`);

        try {
          await sendStatusToKuma(pageInfo.pushToken, result.status, result.msg);
        } catch (err) {
          console.error(`[crawler] Erreur lors de l'envoi du statut pour ${pageInfo.url}:`, err.message);
        }

        // Délai de 2s entre les pages (anti‑429)
        await new Promise(resolve => setTimeout(resolve, 2000));
      }

      await browser.close();
      await markSiteCrawled(site.id);
      console.log(`[crawler] Fin du traitement du site ${site.id}`);
    }

    console.log('[crawler] main() terminée avec succès');
  } catch (err) {
    console.error('[crawler] Erreur dans main():', err);
    throw err;
  }
}

// --- Exécution ---
console.log('[crawler] Appel de main()');
main().catch((err) => {
  console.error('[crawler] Erreur non catchée dans main:', err);
  process.exit(1);
});
console.log('[crawler] main() appelée (asynchrone)');
