// ============================================================
// 5. VÉRIFICATION D'UNE PAGE AVEC OPENCLAW (Décision IA)
// ============================================================

async function checkPage(context, url) {
  const page = await context.newPage();
  const navStart = Date.now();
  
  // Collecte des preuves (identique à votre version)
  const apiRequests = [];
  const failedRequests = [];
  let apiDataReceived = false;
  let apiDataHasContent = false;
  let apiDataCount = 0;
  let uiContent = { textLength: 0, elementCount: 0, hasContent: false };
  let jsErrors = [];
  let consoleErrors = [];
  let responseBodies = [];

  // ---- Listeners (identiques à votre version) ----
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

      // Capturer les corps de réponse pour OpenClaw
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

      // Détection des erreurs silencieuses (votre logique)
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
    // Chargement initial
    const response = await page.goto(url, { waitUntil: 'load', timeout: 30000 });
    if (!response || !response.ok()) {
      return { 
        status: 'down', 
        msg: `HTTP ${response ? response.status() : 'no response'}`,
        loadTimeMs: Date.now() - navStart
      };
    }

    // Attente initiale + scroll + clics (identique à votre version)
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

      // Clics intelligents (copié de votre version)
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

    // ---- Récupération du DOM final ----
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

    // ---- PRÉPARATION DES PREUVES POUR OPENCLAW ----
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

    // ---- DÉCISION PAR OPENCLAW (appel CLI) ----
    console.log('[crawler] Demande de décision à OpenClaw...');
    const verdict = await askOpenClaw(evidence);
    
    console.log(`[crawler] Verdict OpenClaw : ${verdict.status} - ${verdict.reason}`);
    return { 
      status: verdict.status, 
      msg: verdict.reason,
      loadTimeMs,
      openclawAnalysis: verdict.details
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
// 5b. APPEL À OPENCLAW (via CLI)
// ============================================================

async function askOpenClaw(evidence) {
  // Si OpenClaw n'est pas installé, fallback sur la logique existante
  const hasOpenClaw = await checkIfOpenClawAvailable();
  if (!hasOpenClaw) {
    console.warn('[crawler] OpenClaw non disponible, fallback sur logique existante');
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
    // Appel à OpenClaw en mode CLI (il utilise l'IA configurée)
    const { exec } = require('child_process');
    const result = await new Promise((resolve, reject) => {
      // On passe le prompt à OpenClaw via la commande `openclaw run`
      const cmd = `openclaw run "analyse-site" --prompt "${prompt.replace(/"/g, '\\"')}" --output json`;
      exec(cmd, { timeout: 60000, maxBuffer: 10 * 1024 * 1024 }, (error, stdout, stderr) => {
        if (error) {
          reject(new Error(`OpenClaw failed: ${stderr}`));
        } else {
          try {
            resolve(JSON.parse(stdout));
          } catch (e) {
            reject(new Error(`Invalid JSON from OpenClaw: ${stdout}`));
          }
        }
      });
    });
    return result;
  } catch (error) {
    console.error('[crawler] Erreur OpenClaw:', error.message);
    return fallbackDecision(evidence);
  }
}

// Vérifier si OpenClaw est disponible
async function checkIfOpenClawAvailable() {
  const { exec } = require('child_process');
  try {
    await new Promise((resolve, reject) => {
      exec('openclaw --version', (error) => {
        if (error) reject(error);
        else resolve();
      });
    });
    return true;
  } catch {
    return false;
  }
}

// Logique de fallback (votre ancienne logique)
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
