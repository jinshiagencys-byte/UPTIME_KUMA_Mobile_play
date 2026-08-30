const { chromium } = require('playwright');

// ============================================================
// 1. CONFIGURATION (variables d'environnement)
// ============================================================

const RELAY_URL = process.env.RELAY_URL;
const RELAY_SECRET = process.env.RELAY_SECRET;
const KUMA_URL = process.env.KUMA_URL;

// Timeouts et seuils
const API_WAIT_TIMEOUT_MS = parseInt(process.env.API_WAIT_TIMEOUT_MS || '30000', 10);
const MAX_LOAD_TIME_MS = parseInt(process.env.MAX_LOAD_TIME_MS || '10000', 10);
const MIN_TEXT_LENGTH = parseInt(process.env.MIN_TEXT_LENGTH || '100', 10);
const MAX_CLICKS = parseInt(process.env.MAX_CLICKS || '3', 10);        // nombre max de clics dans le parcours
const ENABLE_USER_FLOW = process.env.ENABLE_USER_FLOW !== 'false';   // activé par défaut
const ENABLE_SCROLL = process.env.ENABLE_SCROLL !== 'false';

const IGNORED_DOMAINS = (process.env.IGNORED_DOMAINS || 
  'google-analytics.com,doubleclick.net,facebook.net,cdn.jsdelivr.net,google.com,googleapis.com,gstatic.com')
  .split(',')
  .map(d => d.trim());

console.log('[crawler] Début du script');
console.log(`[crawler] Configuration : API_WAIT=${API_WAIT_TIMEOUT_MS}ms, MAX_LOAD=${MAX_LOAD_TIME_MS}ms, USER_FLOW=${ENABLE_USER_FLOW}`);

// ============================================================
// 2. UTILITAIRES
// ============================================================

function getHostname(url) {
  try { return new URL(url).hostname; } catch { return ''; }
}

function isIgnoredDomain(url) {
  const hostname = getHostname(url);
  return IGNORED_DOMAINS.some(domain => hostname.includes(domain));
}

// Détecte si une réponse JSON contient des données exploitables
function checkIfDataPresent(json) {
  if (!json) return false;
  if (Array.isArray(json) && json.length > 0) return true;
  // Cas standard : { data: [...] }
  for (const key of ['data', 'items', 'results', 'products', 'users', 'posts', 'list']) {
    if (json[key] && Array.isArray(json[key]) && json[key].length > 0) return true;
  }
  // Objet non vide sans propriété d'erreur
  if (typeof json === 'object' && !Array.isArray(json)) {
    const keys = Object.keys(json);
    if (keys.length > 0 && !json.error && !json.message && json.success !== false) {
      return true;
    }
  }
  return false;
}

// Compte le nombre d'éléments dans une réponse API
function countDataItems(json) {
  if (!json) return 0;
  if (Array.isArray(json)) return json.length;
  for (const key of ['data', 'items', 'results', 'products', 'users', 'posts', 'list']) {
    if (json[key] && Array.isArray(json[key])) return json[key].length;
  }
  return 0;
}

// ============================================================
// 3. APPELS HTTP VERS LE RELAY
// ============================================================

async function relayGet(path) {
  const url = `${RELAY_URL}${path}`;
  const res = await fetch(url, { headers: { 'x-relay-secret': RELAY_SECRET } });
  if (!res.ok) throw new Error(`HTTP ${res.status} sur ${url}`);
  return res.json();
}

async function relayPost(path, body = {}) {
  const url = `${RELAY_URL}${path}`;
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'x-relay-secret': RELAY_SECRET, 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} sur ${url}`);
  return res.json();
}

// ============================================================
// 4. FONCTIONS SPÉCIFIQUES (sites, tokens, etc.)
// ============================================================

async function getPagesForSite(groupId) {
  const data = await relayGet(`/push-tokens?groupId=${groupId}`);
  if (!data.success) throw new Error('Erreur récupération pages');
  return data.tokens;
}

function isDue(site) {
  const last = site.last_crawled_at ? new Date(site.last_crawled_at) : null;
  const interval = site.crawl_interval_minutes || 1440;
  const now = new Date();
  const due = !last || (now - last) / 60000 >= interval;
  console.log(`[isDue] ${site.client_name} (${site.id}) : due=${due}`);
  return due;
}

async function markSiteCrawled(siteId) {
  try {
    await relayPost(`/sites/${siteId}/mark-crawled`, {});
    console.log(`[crawler] Site ${siteId} marqué crawlé.`);
  } catch (err) {
    console.error(`[crawler] Erreur mark-crawled ${siteId} :`, err.message);
  }
}

async function sendStatusToKuma(pushToken, status, msg) {
  const url = `${KUMA_URL}/api/push/${pushToken}?status=${status === 'up' ? 'up' : 'down'}&msg=${encodeURIComponent(msg || '')}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} lors du push Kuma`);
}

// ============================================================
// 5. VÉRIFICATION D'UNE PAGE (avec parcours utilisateur)
// ============================================================

async function checkPage(context, url) {
  const page = await context.newPage();
  const navStart = Date.now();
  let status = 'up';
  let msg = 'OK';
  let loadTimeMs = 0;

  // Collecte des erreurs et requêtes
  const apiRequests = [];
  const failedRequests = [];
  let apiDataReceived = false;
  let apiDataHasContent = false;
  let apiDataCount = 0;
  let uiContent = { textLength: 0, elementCount: 0, hasContent: false };
  let jsErrors = [];
  let consoleErrors = [];

  // ---- Listeners ----

  // Requêtes XHR/fetch
  page.on('request', (request) => {
    if (request.resourceType() === 'xhr' || request.resourceType() === 'fetch') {
      const reqUrl = request.url();
      if (!isIgnoredDomain(reqUrl)) {
        apiRequests.push({ url: reqUrl, method: request.method() });
      }
    }
  });

  // Réponses : analyse des statuts et du contenu JSON
  page.on('response', async (response) => {
    const req = response.request();
    if (req.resourceType() === 'xhr' || req.resourceType() === 'fetch') {
      const url = response.url();
      if (isIgnoredDomain(url)) return;
      const statusCode = response.status();

      if (statusCode >= 500) {
        failedRequests.push({ url, status: statusCode, type: 'http_error' });
        return;
      }

      // Analyser les réponses JSON (même en 200, on vérifie le contenu)
      if (statusCode === 200 && (url.includes('/api/') || url.includes('/graphql'))) {
        try {
          const json = await response.json().catch(() => null);
          if (json) {
            apiDataReceived = true;
            // Détection d'erreur silencieuse (success: false, error: "...")
            if (json.success === false || json.error || json.message) {
              failedRequests.push({
                url,
                status: 200,
                type: 'silent_error',
                message: json.error || json.message || 'success=false'
              });
            } else {
              const hasData = checkIfDataPresent(json);
              if (hasData) {
                apiDataHasContent = true;
                apiDataCount += countDataItems(json);
              } else {
                // Réponse 200 mais sans données → considéré comme échec
                failedRequests.push({
                  url,
                  status: 200,
                  type: 'empty_data',
                  message: 'JSON valide mais sans données'
                });
              }
            }
          }
        } catch (e) { /* pas du JSON, on ignore */ }
      }
    }
  });

  // Échecs réseau (DNS, connexion)
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

  // Erreurs JavaScript non catchées
  page.on('pageerror', (error) => {
    jsErrors.push(error.message);
    failedRequests.push({
      type: 'js_error',
      message: error.message
    });
  });

  // Erreurs de console (console.error)
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      const text = msg.text();
      consoleErrors.push(text);
      failedRequests.push({
        type: 'console_error',
        message: text
      });
    }
  });

  // ---- Exécution du test ----

  try {
    // 5a. Chargement initial
    const response = await page.goto(url, { waitUntil: 'load', timeout: 30000 });
    if (!response || !response.ok()) {
      status = 'down';
      msg = `HTTP ${response ? response.status() : 'no response'}`;
    } else {
      // 5b. Attente des requêtes API (premier délai)
      console.log(`[crawler] Attente initiale ${API_WAIT_TIMEOUT_MS}ms pour les requêtes...`);
      await page.waitForTimeout(API_WAIT_TIMEOUT_MS);

      // 5c. PARCOURS UTILISATEUR (scroll + clics) si activé
      if (ENABLE_USER_FLOW) {
        console.log('[crawler] Début du parcours utilisateur');

        // Scroll en bas et en haut pour déclencher le lazy loading
        if (ENABLE_SCROLL) {
          await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
          await page.waitForTimeout(2000);
          await page.evaluate(() => window.scrollTo(0, 0));
          await page.waitForTimeout(2000);
          console.log('[crawler] Scroll effectué');
        }

        // Clics sur les premiers liens internes (non destructeurs)
        let clicksDone = 0;
        const clickedUrls = new Set();

        // Sélecteurs : on cible les liens, onglets, boutons qui mènent à du contenu
        const selectors = [
          'a[href*="product"]',
          'a[href*="category"]',
          'a[href*="detail"]',
          'button[aria-label*="next"]',
          'a.tab',
          'li.nav-item a',
          'a[href*="page"]',
          'a[href*="blog"]',
          'button:has-text("Voir plus")'
        ];

        for (const selector of selectors) {
          if (clicksDone >= MAX_CLICKS) break;
          try {
            const elements = await page.$$(selector);
            for (const el of elements) {
              if (clicksDone >= MAX_CLICKS) break;
              const href = await el.getAttribute('href');
              // Éviter les liens externes, les actions de déconnexion, etc.
              if (href) {
                if (href.startsWith('http') && !href.includes(new URL(url).hostname)) continue;
                if (href.includes('logout') || href.includes('delete') || href.includes('remove')) continue;
              }
              // Ne pas cliquer deux fois sur la même URL
              if (href && clickedUrls.has(href)) continue;
              if (href) clickedUrls.add(href);

              // Rendre l'élément visible et cliquer
              await el.scrollIntoViewIfNeeded();
              await el.click({ timeout: 5000 });
              clicksDone++;
              console.log(`[crawler] Clic #${clicksDone} sur ${selector} (${href || 'pas de href'})`);

              // Attendre que les requêtes se déclenchent
              await page.waitForTimeout(3000);
            }
          } catch (e) {
            // Sélecteur non trouvé ou erreur de clic, on continue
          }
        }

        // Dernier délai pour capturer les requêtes déclenchées par les clics
        await page.waitForTimeout(5000);
        console.log(`[crawler] Parcours terminé (${clicksDone} clics effectués)`);
      }

      // 5d. Analyse finale du DOM
      uiContent = await page.evaluate(() => {
        const textContent = document.body.innerText || '';
        const dataElements = document.querySelectorAll(
          '[class*="item"], [class*="product"], [class*="card"], li, .post, .article, tr, .row'
        );
        return {
          textLength: textContent.length,
          elementCount: dataElements.length,
          hasContent: textContent.length > 200 && dataElements.length > 0
        };
      });

      loadTimeMs = Date.now() - navStart;

      // 5e. Synthèse des erreurs
      const errors = [];

      // Lenteur
      if (loadTimeMs > MAX_LOAD_TIME_MS) {
        errors.push(`Chargement trop lent : ${loadTimeMs}ms (seuil ${MAX_LOAD_TIME_MS}ms)`);
      }

      // Absence d'API
      if (apiRequests.length === 0) {
        errors.push('Aucun appel API détecté (site statique ou backend muet)');
      }

      // Échecs variés
      if (failedRequests.length > 0) {
        const details = failedRequests.map(r => {
          if (r.type === 'http_error') return `${r.url} (HTTP ${r.status})`;
          if (r.type === 'empty_data') return `${r.url} (données vides)`;
          if (r.type === 'silent_error') return `${r.url} (silencieux: ${r.message})`;
          if (r.type === 'network_error') return `${r.url} (${r.error})`;
          if (r.type === 'js_error') return `JS error: ${r.message}`;
          if (r.type === 'console_error') return `Console error: ${r.message}`;
          return `${r.url} (${r.type})`;
        });
        errors.push(`Échecs détectés : ${details.join('; ')}`);
      }

      // Contenu UI insuffisant
      if (!uiContent.hasContent) {
        errors.push('Page ne semble pas afficher de données (texte court ou absence d\'éléments)');
      }

      // Cohérence API ↔ UI
      if (apiDataReceived && apiDataHasContent && apiDataCount > 0) {
        const uiCount = uiContent.elementCount;
        if (uiCount < apiDataCount * 0.5) {
          errors.push(`Incohérence : API ${apiDataCount} éléments, UI ${uiCount} affichés`);
        }
      }

      if (errors.length > 0) {
        status = 'down';
        msg = errors.join('; ');
      } else {
        status = 'up';
        msg = 'OK (API, contenu, performance, pas d\'erreur JS)';
      }
    }
  } catch (err) {
    status = 'down';
    msg = String(err.message || err).slice(0, 300);
    loadTimeMs = Date.now() - navStart;
  } finally {
    // ---- LOGS DÉTAILLÉS ----
    const observedDomains = new Set();
    apiRequests.forEach(r => {
      try { observedDomains.add(new URL(r.url).hostname); } catch {}
    });
    console.log(`[crawler] Domaines XHR/fetch : ${Array.from(observedDomains).join(', ') || 'aucun'}`);
    if (failedRequests.length) {
      console.log(`[crawler] Échecs (${failedRequests.length}) :`, failedRequests.map(r => r.url || r.message).join(', '));
    }
    if (jsErrors.length) console.log(`[crawler] Erreurs JS :`, jsErrors.join('; '));
    if (consoleErrors.length) console.log(`[crawler] Erreurs console :`, consoleErrors.join('; '));
    console.log(`[crawler] Temps de chargement : ${loadTimeMs}ms (seuil ${MAX_LOAD_TIME_MS}ms)`);
    console.log(`[crawler] UI : ${apiDataCount} données API, ${uiContent.elementCount} éléments`);
    console.log(`[crawler] Résultat : ${status} - ${msg}`);

    await page.close();
  }

  return { status, msg, loadTimeMs };
}

// ============================================================
// 6. FONCTION PRINCIPALE
// ============================================================

async function main() {
  console.log('[crawler] main() démarrée');
  try {
    const sitesData = await relayGet('/active-sites');
    console.log(`[crawler] Sites actifs : ${sitesData.sites?.length || 0}`);

    if (!sitesData.success || !sitesData.sites || sitesData.sites.length === 0) {
      console.log('[crawler] Aucun site actif. Fin.');
      return;
    }

    const dueSites = sitesData.sites.filter(isDue);
    console.log(`[crawler] Sites dus : ${dueSites.length}`);

    for (const site of dueSites) {
      console.log(`[crawler] --- Traitement de ${site.client_name} (${site.site_url}) ---`);
      let pages = [];
      try {
        pages = await getPagesForSite(site.kuma_group_id);
      } catch (err) {
        console.error(`[crawler] Erreur récupération pages :`, err.message);
        await markSiteCrawled(site.id);
        continue;
      }

      if (pages.length === 0) {
        console.log(`[crawler] Aucune page. Marquage.`);
        await markSiteCrawled(site.id);
        continue;
      }

      const browser = await chromium.launch({ headless: true });
      const context = await browser.newContext({
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
      });

      for (const pageInfo of pages) {
        console.log(`[crawler] Vérification : ${pageInfo.url}`);
        const result = await checkPage(context, pageInfo.url);
        console.log(`[crawler] Résultat : ${result.status} - ${result.msg}`);
        try {
          await sendStatusToKuma(pageInfo.pushToken, result.status, result.msg);
        } catch (err) {
          console.error(`[crawler] Erreur push Kuma :`, err.message);
        }
        await new Promise(resolve => setTimeout(resolve, 2000));
      }

      await browser.close();
      await markSiteCrawled(site.id);
      console.log(`[crawler] --- Fin de ${site.client_name} ---`);
    }

    console.log('[crawler] main() terminée avec succès');
  } catch (err) {
    console.error('[crawler] Erreur fatale dans main() :', err);
    throw err;
  }
}

// ============================================================
// 7. EXÉCUTION
// ============================================================

console.log('[crawler] Appel de main()');
main().catch((err) => {
  console.error('[crawler] Erreur non catchée :', err);
  process.exit(1);
});
console.log('[crawler] main() appelée (asynchrone)');
