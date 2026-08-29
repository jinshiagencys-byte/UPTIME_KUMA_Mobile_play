const { chromium } = require('playwright');

const RELAY_URL = process.env.RELAY_URL;
const RELAY_SECRET = process.env.RELAY_SECRET;
const KUMA_URL = process.env.KUMA_URL;

const NETWORK_IDLE_TIMEOUT_MS = parseInt(process.env.NETWORK_IDLE_TIMEOUT_MS || '15000', 10);
const BLANK_PAGE_THRESHOLD = parseFloat(process.env.BLANK_PAGE_THRESHOLD || '0.9', 10); // 90% pixels identiques
const MIN_TEXT_LENGTH = parseInt(process.env.MIN_TEXT_LENGTH || '100', 10);
const IGNORED_DOMAINS = (process.env.IGNORED_DOMAINS || 'google-analytics.com,doubleclick.net,facebook.net,cdn.jsdelivr.net').split(',').map(d => d.trim());

// Fonctions utilitaires identiques (relayGet, pushStatus, etc.)
// ...

function isIgnoredDomain(url) {
  const hostname = getHostname(url);
  return IGNORED_DOMAINS.some(domain => hostname.includes(domain));
}

async function checkPage(context, url) {
  const page = await context.newPage();
  let status = 'up';
  let msg = 'OK';
  let loadTimeMs = null;
  const navStart = Date.now();

  // Flags pour les différents signaux
  let sawApiError = false;
  let networkIdleReached = false;

  // --- Journal brut de tous les domaines contactés en xhr/fetch ---
  // Aucune supposition ici sur ce qui "devrait" être une API : on note
  // juste le hostname de CHAQUE requête xhr/fetch observée, avec un
  // exemple d'URL complète. C'est purement de l'observation.
  const observedDomains = new Map(); // hostname -> { count, examples: [] }

  function recordObservedRequest(reqUrl) {
    if (isIgnoredDomain(reqUrl)) return;
    let hostname;
    try {
      hostname = new URL(reqUrl).hostname;
    } catch {
      return;
    }
    const entry = observedDomains.get(hostname) || { count: 0, examples: [] };
    entry.count += 1;
    if (entry.examples.length < 3) entry.examples.push(reqUrl);
    observedDomains.set(hostname, entry);
  }

  // Écouteurs réseau (pour erreurs API)
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

  // Nouveau listener : capture toute requête xhr/fetch, réussie ou non,
  // pour construire le journal brut ci-dessus.
  page.on('request', (req) => {
    const type = req.resourceType();
    if (type === 'xhr' || type === 'fetch') {
      recordObservedRequest(req.url());
    }
  });

  try {
    const response = await page.goto(url, { waitUntil: 'load', timeout: 20000 });

    if (!response || !response.ok()) {
      status = 'down';
      msg = `HTTP ${response ? response.status() : 'no response'}`;
    } else {
      // 1. Attendre network idle (avec timeout)
      try {
        await page.waitForLoadState('networkidle', { timeout: NETWORK_IDLE_TIMEOUT_MS });
        networkIdleReached = true;
      } catch (e) {
        networkIdleReached = false;
      }

      // 2. Vérifier le contenu textuel
      const textLength = await page.evaluate(() => document.body.innerText.length);
      const isBlankPage = textLength < MIN_TEXT_LENGTH;

      // 3. Détection d'écran blanc par analyse de pixels (optionnelle mais recommandée)
      let blankByPixels = false;
      try {
        const screenshot = await page.screenshot();
        // Analyse simple : compter les couleurs uniques via buffer (à implémenter)
        // Pour l'exemple, on suppose une fonction analyzeScreenshot(screenshot) qui renvoie true si > BLANK_PAGE_THRESHOLD de pixels identiques
        // blankByPixels = analyzeScreenshot(screenshot);
      } catch (e) {
        // ignore
      }

      // Décision finale
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
    // --- Log du journal brut, quoi qu'il arrive (page up ou down) ---
    // On trie par nombre d'appels décroissant, purement descriptif.
    const sortedDomains = Array.from(observedDomains.entries())
      .sort((a, b) => b[1].count - a[1].count);

    console.log(`[network-log] ${url} — ${sortedDomains.length} domaine(s) xhr/fetch observé(s) :`);
    for (const [hostname, entry] of sortedDomains) {
      console.log(`  - ${hostname} (${entry.count} appel(s)) ex: ${entry.examples.join(', ')}`);
    }
    if (sortedDomains.length === 0) {
      console.log('  (aucune requête xhr/fetch observée)');
    }

    await page.close();
  }

  return { status, msg, loadTimeMs };
}

// Le reste (main, isDue, etc.) identique
