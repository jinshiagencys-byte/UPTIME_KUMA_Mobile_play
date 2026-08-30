const { chromium } = require('playwright');

// ============================================================
// 1. CONFIGURATION
// ============================================================

const RELAY_URL = process.env.RELAY_URL;
const RELAY_SECRET = process.env.RELAY_SECRET;
const KUMA_URL = process.env.KUMA_URL;

const API_WAIT_TIMEOUT_MS = parseInt(process.env.API_WAIT_TIMEOUT_MS || '30000', 10);
const MAX_LOAD_TIME_MS = parseInt(process.env.MAX_LOAD_TIME_MS || '10000', 10);
const MAX_CLICKS = parseInt(process.env.MAX_CLICKS || '3', 10);
const ENABLE_USER_FLOW = process.env.ENABLE_USER_FLOW !== 'false';
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

function checkIfDataPresent(json) {
  if (!json) return false;
  if (Array.isArray(json) && json.length > 0) return true;
  for (const key of ['data', 'items', 'results', 'products', 'users', 'posts', 'list']) {
    if (json[key] && Array.isArray(json[key]) && json[key].length > 0) return true;
  }
  if (typeof json === 'object' && !Array.isArray(json)) {
    const keys = Object.keys(json);
    if (keys.length > 0 && !json.error && !json.message && json.success !== false) {
      return true;
    }
  }
  return false;
}

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
// 4. FONCTIONS SPÉCIFIQUES
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
// 5. VÉRIFICATION D'UNE PAGE AVEC IA (OpenRouter)
// ============================================================

async function checkPage(context, url) {
  const page = await context.newPage();
  const navStart = Date.now();
  
  const apiRequests = [];
  const failedRequests = [];
  let apiDataReceived = false;
  let apiDataHasContent = false;
  let apiDataCount = 0;
  let uiContent = { textLength: 0, elementCount: 0, hasContent: false };
  let jsErrors = [];
  let consoleErrors = [];
  let responseBodies = [];

  // ---- Listeners ----
  page.on('request', (request) => {
    if (request.resourceType() === 'xhr' || request.resourceType() === 'fetch') {
      const reqUrl = request.url();
      if (!isIgnoredDomain(reqUrl)) {
        apiRequests.push({ url: reqUrl, method: request.method() });
      }
    }
  });

  page.on('response', async (response) => {
    const req = response.request();
    if (req.resourceType() === 'xhr' || req.resourceType() === 'fetch') {
      const url = response.url();
      if (isIgnoredDomain(url)) return;
      const statusCode = response.status();

      try {
        const contentType = response.headers()['content-type'] || '';
        if (contentType.includes('json')) {
          const json = await response.json().catch(() => null);
          if (json) {
            responseBodies.push({ url, status: statusCode, body: json });
          }
        }
      } catch (e) { /* ignore */ }

      if (statusCode >= 500) {
        failedRequests.push({ url, status: statusCode, type: 'http_error' });
      }

      if (statusCode === 200 && (url.includes('/api/') || url.includes('/graphql'))) {
        try {
          const json = await response.json().catch(() => null);
          if (json) {
            apiDataReceived = true;
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
                failedRequests.push({
                  url,
                  status: 200,
                  type: 'empty_data',
                  message: 'JSON valide mais sans données'
                });
              }
            }
          }
        } catch (e) { /* pas du JSON */ }
      }
    }
  });

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

  page.on('pageerror', (error) => {
    jsErrors.push(error.message);
  });

  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      consoleErrors.push(msg.text());
    }
  });

  // ---- Exécution du test ----
  try {
    const response = await page.goto(url, { waitUntil: 'load', timeout: 30000 });
    if (!response || !response.ok()) {
      return { 
        status: 'down', 
        msg: `HTTP ${response ? response.status() : 'no response'}`,
        loadTimeMs: Date.now() - navStart
      };
    }

    console.log(`[crawler] Attente initiale ${API_WAIT_TIMEOUT_MS}ms...`);
    await page.waitForTimeout(API_WAIT_TIMEOUT_MS);

    if (ENABLE_USER_FLOW) {
      console.log('[crawler] Début du parcours utilisateur');
      if (ENABLE_SCROLL) {
        await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
        await page.waitForTimeout(2000);
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.waitForTimeout(2000);
      }

      let clicksDone = 0;
      const clickedUrls = new Set();
      const selectors = [
        'a[href*="product"]', 'a[href*="category"]', 'a[href*="detail"]',
        'button[aria-label*="next"]', 'a.tab', 'li.nav-item a',
        'a[href*="page"]', 'a[href*="blog"]', 'button:has-text("Voir plus")'
      ];

      for (const selector of selectors) {
        if (clicksDone >= MAX_CLICKS) break;
        try {
          const elements = await page.$$(selector);
          for (const el of elements) {
            if (clicksDone >= MAX_CLICKS) break;
            const href = await el.getAttribute('href');
            if (href) {
              if (href.startsWith('http') && !href.includes(new URL(url).hostname)) continue;
              if (href.includes('logout') || href.includes('delete') || href.includes('remove')) continue;
              if (href && clickedUrls.has(href)) continue;
              if (href) clickedUrls.add(href);
            }
            await el.scrollIntoViewIfNeeded();
            await el.click({ timeout: 5000 });
            clicksDone++;
            console.log(`[crawler] Clic #${clicksDone} sur ${selector}`);
            await page.waitForTimeout(3000);
          }
        } catch (e) { /* ignore */ }
      }
      await page.waitForTimeout(5000);
      console.log(`[crawler] Parcours terminé (${clicksDone} clics)`);
    }

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

    const loadTimeMs = Date.now() - navStart;

    // ---- PREUVES POUR L'IA ----
    const evidence = {
      url,
      loadTimeMs,
      statusCode: response.status(),
      headers: response.headers(),
      bodyText: await page.locator('body').innerText().catch(() => '').then(t => t.substring(0, 2000)),
      apiRequests: apiRequests.slice(0, 20),
      apiResponses: responseBodies.slice(0, 10).map(r => ({
        url: r.url,
        status: r.status,
        bodyPreview: JSON.stringify(r.body).substring(0, 500)
      })),
      uiContent,
      jsErrors: jsErrors.slice(0, 10),
      consoleErrors: consoleErrors.slice(0, 10),
      failedRequests: failedRequests.slice(0, 10).map(r => ({
        url: r.url || 'N/A',
        type: r.type,
        message: r.message || r.error || r.status || 'unknown'
      }))
    };

    console.log('[crawler] Demande de décision à OpenRouter...');
    const verdict = await askOpenRouter(evidence);
    
    console.log(`[crawler] Verdict IA : ${verdict.status} - ${verdict.reason}`);
    return { 
      status: verdict.status, 
      msg: verdict.reason,
      loadTimeMs,
      details: verdict.details
    };

  } catch (err) {
    return { 
      status: 'down', 
      msg: String(err.message || err).slice(0, 300),
      loadTimeMs: Date.now() - navStart
    };
  } finally {
    await page.close();
  }
}

// ============================================================
// 5b. APPEL DIRECT À OPENROUTER (sans OpenClaw)
// ============================================================

async function askOpenRouter(evidence) {
  const apiKey = process.env.OPENROUTER_API_KEY;
  if (!apiKey) {
    console.warn('[crawler] OPENROUTER_API_KEY non définie, fallback sur logique existante');
    return fallbackDecision(evidence);
  }

  const prompt = `
Tu es un expert en monitoring de sites web. Analyse ces preuves et décide si le site est fonctionnel.

URL: ${evidence.url}
Temps de chargement: ${evidence.loadTimeMs}ms
Code HTTP: ${evidence.statusCode}

Requêtes API: ${evidence.apiRequests.length} détectées
Erreurs réseau/HTTP: ${evidence.failedRequests.length}
Erreurs JS: ${evidence.jsErrors.length}
Erreurs console: ${evidence.consoleErrors.length}

Contenu UI:
- Texte: ${evidence.bodyText.length} caractères
- Éléments: ${evidence.uiContent.elementCount}
- Contenu suffisant: ${evidence.uiContent.hasContent}

Réponses API (extrait):
${evidence.apiResponses.map(r => `- ${r.url}: ${r.status} → ${r.bodyPreview}`).join('\n')}

Erreurs détectées:
${evidence.failedRequests.map(r => `- ${r.type}: ${r.message}`).join('\n')}

Questions:
1. Un utilisateur peut-il utiliser le site normalement ?
2. Y a-t-il des signes de panne silencieuse (API qui répond 200 mais sans données, erreurs JS bloquantes) ?
3. Le site est-il "UP" ou "DOWN" ?

Réponds UNIQUEMENT au format JSON :
{
  "status": "up" ou "down",
  "reason": "une phrase expliquant le verdict",
  "details": "analyse détaillée en 2-3 phrases"
}
`;

  try {
    const response = await fetch('https://openrouter.ai/api/v1/chat/completions', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
        'HTTP-Referer': 'https://github.com/jinshiagencys-byte/UPTIME_KUMA_Mobile_play',
        'X-Title': 'SentinelSite Crawler'
      },
      body: JSON.stringify({
        model: 'openrouter/free',
        messages: [
          { role: 'user', content: prompt }
        ],
        temperature: 0.1,
        response_format: { type: 'json_object' }
      })
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`OpenRouter API error ${response.status}: ${errorText}`);
    }

    const data = await response.json();
    const content = data.choices[0].message.content;
    try {
      return JSON.parse(content);
    } catch (e) {
      console.warn('[crawler] Réponse non-JSON d\'OpenRouter:', content);
      return { status: 'down', reason: 'Réponse invalide de l\'IA', details: content };
    }
  } catch (error) {
    console.error('[crawler] Erreur OpenRouter:', error.message);
    return fallbackDecision(evidence);
  }
}

// ============================================================
// 5c. FALLBACK (ancienne logique)
// ============================================================

function fallbackDecision(evidence) {
  const errors = [];
  if (evidence.failedRequests.length > 0) {
    errors.push(`Échecs détectés: ${evidence.failedRequests.map(r => r.message).join(', ')}`);
  }
  if (evidence.loadTimeMs > MAX_LOAD_TIME_MS) {
    errors.push(`Chargement lent: ${evidence.loadTimeMs}ms`);
  }
  if (!evidence.uiContent.hasContent) {
    errors.push('Contenu UI insuffisant');
  }
  if (evidence.apiRequests.length === 0) {
    errors.push('Aucune requête API détectée');
  }
  if (evidence.jsErrors.length > 0) {
    errors.push(`Erreurs JS: ${evidence.jsErrors.join(', ')}`);
  }
  if (errors.length > 0) {
    return { status: 'down', reason: errors.join('; '), details: 'Fallback logic' };
  }
  return { status: 'up', reason: 'Tous les indicateurs sont verts', details: 'Fallback logic' };
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
// 7. EXPORTS
// ============================================================

module.exports = {
  checkPage,
  relayGet,
  relayPost,
  isDue,
  markSiteCrawled,
  sendStatusToKuma,
  main
};

// ============================================================
// 8. EXÉCUTION DIRECTE
// ============================================================

if (require.main === module) {
  console.log('[crawler] Appel de main()');
  main().catch((err) => {
    console.error('[crawler] Erreur non catchée :', err);
    process.exit(1);
  });
}
