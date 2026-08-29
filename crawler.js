const { chromium } = require('playwright');

// Variables d'environnement
const RELAY_URL = process.env.RELAY_URL;
const RELAY_SECRET = process.env.RELAY_SECRET;
const KUMA_URL = process.env.KUMA_URL;

// Paramètres de configuration
const NETWORK_IDLE_TIMEOUT_MS = parseInt(process.env.NETWORK_IDLE_TIMEOUT_MS || '15000', 10);
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

// --- Vérification d'une page (avec Playwright) avec listener de requêtes ---

async function checkPage(context, url) {
  const page = await context.newPage();
  let status = 'up';
  let msg = 'OK';
  let loadTimeMs = null;
  const navStart = Date.now();

  let sawApiError = false;
  let networkIdleReached = false;

  // --- NOUVEAU : collecte des domaines observés pour les requêtes XHR/fetch ---
  const observedDomains = new Set();
  const observedUrls = [];

  page.on('request', (request) => {
    const resourceType = request.resourceType();
    if (resourceType === 'xhr' || resourceType === 'fetch') {
      const reqUrl = request.url();
      if (!isIgnoredDomain(reqUrl)) {
        const hostname = getHostname(reqUrl);
        observedDomains.add(hostname);
        observedUrls.push(reqUrl);
        // On peut logger en temps réel si besoin, mais on le fera en fin de check
      }
    }
  });

  page.on('response', (response) => {
    if (response.status() >= 500 && !isIgnoredDomain(response.url())) {
      sawApiError = true;
    }
  });
  page.on('requestfailed', (req) => {
    if (req.resourceType() === 'xhr' || req.resourceType() === 'fetch') {
      if (!isIgnoredDomain(req.url())) {
        sawApiError = true;
      }
    }
  });

  try {
    const response = await page.goto(url, { waitUntil: 'load', timeout: 20000 });

    if (!response || !response.ok()) {
      status = 'down';
      msg = `HTTP ${response ? response.status() : 'no response'}`;
    } else {
      try {
        await page.waitForLoadState('networkidle', { timeout: NETWORK_IDLE_TIMEOUT_MS });
        networkIdleReached = true;
      } catch (e) {
        networkIdleReached = false;
      }

      const textLength = await page.evaluate(() => document.body.innerText.length);
      const isBlankPage = textLength < MIN_TEXT_LENGTH;

      if (sawApiError) {
        status = 'down';
        msg = 'Erreur API détectée (HTTP ≥500 ou échec réseau)';
      } else if (!networkIdleReached) {
        status = 'down';
        msg = `Réseau jamais au repos après ${NETWORK_IDLE_TIMEOUT_MS}ms (backend muet ?)`;
      } else if (isBlankPage) {
        status = 'down';
        msg = 'Page vide / écran blanc';
      }
    }

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
    // --- LOG des domaines observés ---
    if (observedDomains.size > 0) {
      console.log(`[crawler] Domaines XHR/fetch observés pour ${url} :`, Array.from(observedDomains).join(', '));
      console.log(`[crawler] URLs complètes (échantillon) :`, observedUrls.slice(0, 5).join(', '));
    } else {
      console.log(`[crawler] Aucune requête XHR/fetch observée pour ${url}`);
    }
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
