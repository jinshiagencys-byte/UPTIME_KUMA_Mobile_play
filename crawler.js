async function checkPage(context, url, siteUrl) {
  const page = await context.newPage();
  let status = 'up';
  let msg = 'OK';
  let loadTimeMs = null;
  const navStart = Date.now();

  // Collecte des requêtes XHR/fetch
  const apiRequests = [];
  const apiResponses = [];
  const failedRequests = [];
  let apiDataReceived = false;
  let apiDataHasContent = false;
  let apiDataCount = 0;

  // Intercepter les requêtes API
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

  // Intercepter les réponses API (pour vérifier le contenu)
  page.on('response', async (response) => {
    const req = response.request();
    if (req.resourceType() === 'xhr' || req.resourceType() === 'fetch') {
      const url = response.url();
      if (!isIgnoredDomain(url)) {
        const statusCode = response.status();
        apiResponses.push({
          url,
          status: statusCode,
          time: Date.now()
        });

        // Vérifier les erreurs HTTP
        if (statusCode >= 500) {
          failedRequests.push({ url, status: statusCode, type: 'http_error' });
          return;
        }

        // Vérifier le contenu des réponses JSON (si c'est une API)
        if (statusCode === 200 && (url.includes('/api/') || url.includes('/graphql'))) {
          try {
            const json = await response.json().catch(() => null);
            if (json) {
              apiDataReceived = true;
              
              // Vérifier si la réponse contient des données
              const hasData = checkIfDataPresent(json);
              if (hasData) {
                apiDataHasContent = true;
                apiDataCount += countDataItems(json);
              } else {
                // L'API a répondu 200 mais sans données → alerte
                failedRequests.push({ 
                  url, 
                  status: 200, 
                  type: 'empty_data',
                  message: 'API a répondu 200 mais données vides'
                });
              }
            }
          } catch (e) {
            // Impossible de parser le JSON → ce n'est pas une API JSON
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
      // Attendre que les requêtes API se terminent (avec timeout)
      const API_WAIT_MS = parseInt(process.env.API_WAIT_TIMEOUT_MS || '30000', 10);
      await page.waitForTimeout(API_WAIT_MS);

      // Vérifier le contenu affiché dans le DOM
      const uiContent = await page.evaluate(() => {
        // Récupérer tous les textes visibles
        const textContent = document.body.innerText || '';
        // Compter les éléments qui ressemblent à des données (li, div avec des classes)
        const dataElements = document.querySelectorAll('[class*="item"], [class*="product"], [class*="card"], li, .post, .article');
        return {
          textLength: textContent.length,
          elementCount: dataElements.length,
          hasContent: textContent.length > 200 && dataElements.length > 0
        };
      });

      // Décision finale basée sur plusieurs critères
      const errors = [];

      // 1. Y a-t-il eu des requêtes API ?
      if (apiRequests.length === 0) {
        errors.push('Aucun appel API détecté (site statique ou backend muet)');
      }

      // 2. Les requêtes API ont-elles toutes réussi ?
      if (failedRequests.length > 0) {
        const errorMessages = failedRequests.map(r => 
          r.type === 'empty_data' ? `${r.url} (données vides)` :
          r.type === 'http_error' ? `${r.url} (HTTP ${r.status})` :
          `${r.url} (${r.error || 'échec réseau'})`
        );
        errors.push(`Échecs détectés : ${errorMessages.join(', ')}`);
      }

      // 3. Les données sont-elles bien affichées dans l'UI ?
      if (!uiContent.hasContent) {
        errors.push('Page ne semble pas afficher de données (texte court ou absence d\'éléments)');
      }

      // 4. Vérifier la cohérence API ↔ UI (si on a des données API)
      if (apiDataReceived && apiDataHasContent && apiDataCount > 0) {
        // On compare le nombre d'éléments affichés avec le nombre retourné par l'API
        const uiElementCount = uiContent.elementCount;
        if (uiElementCount < apiDataCount * 0.5) {
          errors.push(`Incohérence : API retourne ${apiDataCount} éléments, mais seulement ${uiElementCount} affichés`);
        }
      }

      // Décision finale
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
    // Log des domaines observés
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
    console.log(`[crawler] UI : ${apiDataCount} données API, ${uiContent?.elementCount || 0} éléments affichés`);
    
    await page.close();
  }

  return { status, msg, loadTimeMs };
}

// Fonction utilitaire pour vérifier si une réponse JSON contient des données
function checkIfDataPresent(json) {
  if (!json) return false;
  
  // Si c'est un tableau non vide
  if (Array.isArray(json) && json.length > 0) return true;
  
  // Si c'est un objet avec une propriété "data" qui est un tableau non vide
  if (json.data && Array.isArray(json.data) && json.data.length > 0) return true;
  if (json.items && Array.isArray(json.items) && json.items.length > 0) return true;
  if (json.results && Array.isArray(json.results) && json.results.length > 0) return true;
  if (json.products && Array.isArray(json.products) && json.products.length > 0) return true;
  
  // Si c'est un objet non vide avec des propriétés
  if (typeof json === 'object' && !Array.isArray(json)) {
    const keys = Object.keys(json);
    if (keys.length > 0) {
      // Vérifier qu'il n'y a pas un message d'erreur
      if (json.error || json.message) return false;
      return true;
    }
  }
  
  return false;
}

// Fonction utilitaire pour compter les éléments dans une réponse JSON
function countDataItems(json) {
  if (!json) return 0;
  if (Array.isArray(json)) return json.length;
  if (json.data && Array.isArray(json.data)) return json.data.length;
  if (json.items && Array.isArray(json.items)) return json.items.length;
  if (json.results && Array.isArray(json.results)) return json.results.length;
  if (json.products && Array.isArray(json.products)) return json.products.length;
  return 0;
}
