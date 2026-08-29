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
    await page.close();
  }

  return { status, msg, loadTimeMs };
}

// Le reste (main, isDue, etc.) identique
