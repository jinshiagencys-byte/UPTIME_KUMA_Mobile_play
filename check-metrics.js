// check-metrics.js
//
// Script standalone (Node + Playwright natif `tls`) lancé UNE FOIS PAR PAGE
// détectée : la page principale (avant OpenClaw, comme avant) ET chaque
// page découverte par OpenClaw pendant sa patrouille (après OpenClaw,
// boucle sur pages.json côté workflow). Mesure :
//   1. Expiration du certificat SSL (via tls.connect)
//   2. Temps de chargement réel de la page (Navigation Timing API)
//
// Résultat envoyé au relay :
//   - toujours via POST /sites/:id/pages/update-metrics (upsert par
//     (site_id, url) — crée la page si OpenClaw l'a découverte
//     dynamiquement et qu'elle n'existait pas encore)
//   - EN PLUS, si isMainPage=true, via POST /sites/:id/update-metrics
//     (conserve l'ancien comportement pour la carte "groupe"/résumé site)
//
// Usage : node check-metrics.js <siteId> <pageUrl> [isMainPage]
//   isMainPage : "true" pour la page d'accueil (défaut: false)
// Variables d'env : RELAY_URL (défaut https://kuma-relay.up.railway.app),
//                    RELAY_SECRET (obligatoire)

const tls = require('tls');
const { URL } = require('url');
const { chromium } = require('playwright');

const RELAY_URL = process.env.RELAY_URL || 'https://kuma-relay.up.railway.app';
const RELAY_SECRET = process.env.RELAY_SECRET || '';
const TLS_TIMEOUT_MS = 8000;
const PAGE_LOAD_TIMEOUT_MS = 20000;

function log(...args) {
  console.log('[check-metrics]', ...args);
}

// ─── SSL ────────────────────────────────────────────────────────────────────
function getTlsExpiry(pageUrl) {
  return new Promise((resolve) => {
    let hostname;
    try {
      hostname = new URL(pageUrl).hostname;
    } catch {
      return resolve(null);
    }

    const socket = tls.connect(
      { host: hostname, port: 443, servername: hostname, timeout: TLS_TIMEOUT_MS, rejectUnauthorized: false },
      () => {
        try {
          const cert = socket.getPeerCertificate();
          if (!cert || !cert.valid_to) {
            socket.end();
            return resolve(null);
          }
          const validTo = new Date(cert.valid_to);
          const daysRemaining = Math.ceil((validTo.getTime() - Date.now()) / (1000 * 60 * 60 * 24));
          const issuer = cert.issuer && (cert.issuer.O || cert.issuer.CN) ? (cert.issuer.O || cert.issuer.CN) : null;
          socket.end();
          resolve({ validTo: validTo.toISOString(), daysRemaining, issuer });
        } catch (err) {
          socket.end();
          resolve(null);
        }
      }
    );
    socket.on('error', () => resolve(null));
    socket.on('timeout', () => {
      socket.destroy();
      resolve(null);
    });
  });
}

// ─── Temps de chargement ────────────────────────────────────────────────────
async function getLoadTimeMs(pageUrl) {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({
      userAgent:
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    });
    const page = await context.newPage();
    const start = Date.now();
    await page.goto(pageUrl, { waitUntil: 'load', timeout: PAGE_LOAD_TIMEOUT_MS });
    let loadTimeMs = null;
    try {
      loadTimeMs = await page.evaluate(() => {
        const nav = performance.getEntriesByType('navigation')[0];
        return nav ? Math.round(nav.loadEventEnd) : null;
      });
    } catch {
      // Navigation Timing indisponible — on garde le fallback ci-dessous
    }
    if (loadTimeMs == null || loadTimeMs <= 0) {
      loadTimeMs = Date.now() - start; // fallback simple si l'API échoue
    }
    return loadTimeMs;
  } catch (err) {
    log('Erreur chargement page pour temps de réponse :', err.message);
    return null;
  } finally {
    await browser.close();
  }
}

// ─── Envoi au relay ─────────────────────────────────────────────────────────
async function postJson(path, body) {
  const res = await fetch(`${RELAY_URL}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'x-relay-secret': RELAY_SECRET },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => null);
  if (!res.ok || !data || data.success !== true) {
    throw new Error(`Relay a refusé la mise à jour (status ${res.status}) sur ${path}: ${JSON.stringify(data)}`);
  }
}

async function sendMetrics(siteId, pageUrl, isMainPage, metrics) {
  if (!RELAY_SECRET) {
    log('⚠️ RELAY_SECRET manquant, envoi annulé.');
    return;
  }

  // Toujours : métriques au niveau de la page (upsert par site_id+url)
  await postJson(`/sites/${siteId}/pages/update-metrics`, { url: pageUrl, ...metrics });

  // En plus, si c'est la page principale : ancien comportement conservé
  // pour la carte "groupe" (MonitorItem.sslValidTo / loadTimeMs)
  if (isMainPage) {
    await postJson(`/sites/${siteId}/update-metrics`, metrics);
  }
}

// ─── Main ───────────────────────────────────────────────────────────────────
async function main() {
  const [siteId, pageUrl, isMainPageArg] = process.argv.slice(2);
  if (!siteId || !pageUrl) {
    console.error('Usage: node check-metrics.js <siteId> <pageUrl> [isMainPage]');
    process.exit(1);
  }
  const isMainPage = isMainPageArg === 'true';

  log(`Site ${siteId} — page ${pageUrl} (${isMainPage ? 'principale' : 'secondaire'}) — mesure SSL + temps de chargement...`);

  const [tlsInfo, loadTimeMs] = await Promise.all([getTlsExpiry(pageUrl), getLoadTimeMs(pageUrl)]);

  const metrics = {
    sslValidTo: tlsInfo ? tlsInfo.validTo : null,
    sslDaysRemaining: tlsInfo ? tlsInfo.daysRemaining : null,
    sslIssuer: tlsInfo ? tlsInfo.issuer : null,
    loadTimeMs,
  };

  log('Résultat :', metrics);

  try {
    await sendMetrics(siteId, pageUrl, isMainPage, metrics);
    log('✅ Métriques envoyées au relay.');
  } catch (err) {
    log('❌ Échec envoi au relay :', err.message);
    process.exitCode = 1; // n'interrompt pas le job si appelé avec `|| true`
  }
}

main();
