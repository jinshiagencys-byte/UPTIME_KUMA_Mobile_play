const { chromium } = require('playwright');
const axios = require('axios');

// Variables d'environnement
const RELAY_URL = process.env.RELAY_URL;
const RELAY_SECRET = process.env.RELAY_SECRET;
const KUMA_URL = process.env.KUMA_URL;

// Paramètres de configuration (avec valeurs par défaut)
const NETWORK_IDLE_TIMEOUT_MS = parseInt(process.env.NETWORK_IDLE_TIMEOUT_MS || '15000', 10);
const BLANK_PAGE_THRESHOLD = parseFloat(process.env.BLANK_PAGE_THRESHOLD || '0.9', 10);
const MIN_TEXT_LENGTH = parseInt(process.env.MIN_TEXT_LENGTH || '100', 10);
const IGNORED_DOMAINS = (process.env.IGNORED_DOMAINS || 'google-analytics.com,doubleclick.net,facebook.net,cdn.jsdelivr.net').split(',').map(d => d.trim());

console.log('[crawler] Début du script');

// --- Fonctions utilitaires ---

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

// Requête GET vers le relay (avec secret)
async function relayGet(path) {
  const url = `${RELAY_URL}${path}`;
  const response = await axios.get(url, {
    headers: { 'x-relay-secret': RELAY_SECRET }
  });
  return response.data;
}

// Requête POST vers le relay (pour pousser le statut)
async function pushStatus(monitorId, status, msg, ping) {
  const url = `${KUMA_URL}/api/push/${monitorId}`; // Utilise le push endpoint de Kuma
  // Ici on utilise la méthode push via l'API Kuma (push token)
  // Dans notre cas, on a les push tokens stockés en base, on peut les utiliser.
  // Mais le crawler utilise le push token pour envoyer le statut.
  // Il faut récupérer le push token pour chaque monitor. On va les obtenir via une route du relay.
  // Pour simplifier, on va utiliser la route /push-tokens?groupId=...
  // On va récupérer les push tokens en amont.
  // Je propose de modifier la fonction pour qu'elle prenne un pushToken en paramètre.
  // On va plutôt utiliser une autre approche : on a les monitors PUSH créés avec un token,
  // on peut les envoyer directement à Kuma.
  // Mais pour l'instant, je vais laisser en place une implémentation qui utilise l'API Kuma push.
  // On suppose qu'on a le pushToken.
  // Je vais réécrire cette fonction pour utiliser le push token.
}

// Fonction pour récupérer les pages (push tokens) d'un site via son groupId
async function getPagesForSite(groupId) {
  const data = await relayGet(`/push-tokens?groupId=${groupId}`);
  if (!data.success) {
    throw new Error('Erreur lors de la récupération des pages');
  }
  return data.tokens; // tableau d'objets { url, monitorId, pushToken, name }
}

// Fonction isDue
function isDue(site) {
  const last = site.last_crawled_at ? new Date(site.last_crawled_at) : null;
  const intervalMinutes = site.crawl_interval_minutes || 1440; // défaut 24h
  const now = new Date();
  const due = !last || (now - last) / 60000 >= intervalMinutes;
  console.log(`[isDue] site ${site.id} (${site.client_name}) : last_crawled_at=${last}, interval=${intervalMinutes}min, due=${due}`);
  return due;
}

// --- checkPage (fournie précédemment) ---
async function checkPage(context, url) {
  const page = await context.newPage();
  let status = 'up';
  let msg = 'OK';
  let loadTimeMs = null;
  const navStart = Date.now();

  let sawApiError = false;
  let networkIdleReached = false;

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

      let blankByPixels = false;
      // (optionnel) on pourrait ajouter l'analyse de pixels si nécessaire

      if (sawApiError) {
        status = 'down';
        msg = 'Erreur API détectée (HTTP ≥500 ou échec réseau)';
      } else if (!networkIdleReached) {
        status = 'down';
        msg = `Réseau jamais au repos après ${NETWORK_IDLE_TIMEOUT_MS}ms (backend muet ?)`;
      } else if (isBlankPage || blankByPixels) {
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
    await page.close();
  }

  return { status, msg, loadTimeMs };
}

// --- Fonction main ---
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

    // 3. Pour chaque site, lancer le crawler
    for (const site of dueSites) {
      console.log(`[crawler] Traitement du site ${site.id} : ${site.client_name} (${site.site_url})`);

      // Récupérer les pages (push tokens) associées au groupe
      let pages = [];
      try {
        pages = await getPagesForSite(site.kuma_group_id);
        console.log(`[crawler] ${pages.length} pages trouvées pour le groupe ${site.kuma_group_id}`);
      } catch (err) {
        console.error(`[crawler] Erreur lors de la récupération des pages pour le site ${site.id}:`, err.message);
        continue; // passer au site suivant
      }

      if (pages.length === 0) {
        console.log(`[crawler] Aucune page à vérifier pour le site ${site.id}.`);
        // Marquer quand même comme crawlé pour éviter de le retraiter en boucle ?
        // On va le marquer.
        await markSiteCrawled(site.id);
        continue;
      }

      // Créer un contexte Playwright partagé pour ce site (performance)
      const browser = await chromium.launch({ headless: true });
      const context = await browser.newContext({
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
      });
      let allOk = true;

      for (const pageInfo of pages) {
        console.log(`[crawler] Vérification de la page : ${pageInfo.url}`);
        const result = await checkPage(context, pageInfo.url);
        console.log(`[crawler] Résultat pour ${pageInfo.url} : status=${result.status}, msg=${result.msg}`);

        // Envoyer le statut à Kuma via le push token
        try {
          await sendStatusToKuma(pageInfo.pushToken, result.status, result.msg);
        } catch (err) {
          console.error(`[crawler] Erreur lors de l'envoi du statut pour ${pageInfo.url}:`, err.message);
          allOk = false;
        }

        // Petit délai entre les pages pour éviter les 429
        await new Promise(resolve => setTimeout(resolve, 2000));
      }

      await browser.close();

      // Marquer le site comme crawlé (last_crawled_at = maintenant)
      await markSiteCrawled(site.id);

      console.log(`[crawler] Fin du traitement du site ${site.id}`);
    }

    console.log('[crawler] main() terminée avec succès');
  } catch (err) {
    console.error('[crawler] Erreur dans main():', err);
    throw err; // pour être capturé par le catch global
  }
}

// Fonction pour marquer un site comme crawlé
async function markSiteCrawled(siteId) {
  try {
    await axios.post(`${RELAY_URL}/sites/${siteId}/mark-crawled`, {}, {
      headers: { 'x-relay-secret': RELAY_SECRET }
    });
    console.log(`[crawler] Site ${siteId} marqué comme crawlé.`);
  } catch (err) {
    console.error(`[crawler] Erreur lors du marquage du site ${siteId}:`, err.message);
  }
}

// Fonction pour envoyer le statut à Kuma via push token
async function sendStatusToKuma(pushToken, status, msg) {
  // L'URL de push de Kuma est généralement : https://kuma-url/api/push/<token>?status=up&msg=...
  // On peut aussi envoyer un ping (temps de réponse) si on le souhaite.
  // Ici on envoie juste le statut.
  const kumaPushUrl = `${KUMA_URL}/api/push/${pushToken}`;
  const params = new URLSearchParams();
  params.append('status', status === 'up' ? 'up' : 'down');
  if (msg) params.append('msg', msg);
  // On pourrait ajouter ping=... mais on ne le mesure pas pour l'instant.
  await axios.get(kumaPushUrl, { params, timeout: 10000 });
}

// --- Appel de main avec gestion d'erreur ---
console.log('[crawler] Appel de main()');
main().catch((err) => {
  console.error('[crawler] Erreur non catchée dans main:', err);
  process.exit(1);
});
console.log('[crawler] main() appelée (asynchrone)');
