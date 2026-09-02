// check-metrics.js
//
// Script standalone (Node + Playwright natif `tls`) lancé une fois par site,
// AVANT qu'OpenClaw ne patrouille. Mesure :
//   1. Expiration du certificat SSL (via tls.connect, comme le fait déjà
//      kuma-relay pour /monitors/:id — même logique, reprise ici pour ne
//      plus dépendre de Kuma)
//   2. Temps de chargement réel de l'URL principale (via l'API Navigation
//      Timing du navigateur, comme le fait déjà crawler.js pour le `ping`
//      envoyé à Kuma)
//
// Résultat envoyé au relay via POST /sites/:id/update-metrics.
//
// Usage : node check-metrics.js <siteId> <siteUrl>
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
// Reprend exactement la logique déjà utilisée côté relay (index.js,
// getTlsExpiry) pour rester cohérent avec ce qui existait avant.
function getTlsExpiry(siteUrl) {
  return new Promise((resolve) => {
    let hostname;
    try {
      hostname = new URL(siteUrl).hostname;
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
// Reprend le même principe que crawler.js (Navigation Timing API), mesuré
// uniquement sur l'URL principale du site.
async function getLoadTimeMs(siteUrl) {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({
      userAgent:
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    });
    const page = await context.newPage();
    const start = Date.now();
    await page.goto(siteUrl, { waitUntil: 'load', timeout: PAGE_LOAD_TIMEOUT_MS });
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
async function sendMetrics(siteId, metrics) {
  if (!RELAY_SECRET) {
    log('⚠️ RELAY_SECRET manquant, envoi annulé.');
    return;
  }
  const res = await fetch(`${RELAY_URL}/sites/${siteId}/update-metrics`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'x-relay-secret': RELAY_SECRET },
    body: JSON.stringify(metrics),
  });
  const data = await res.json().catch(() => null);
  if (!res.ok || !data || data.success !== true) {
    throw new Error(`Relay a refusé la mise à jour (status ${res.status}): ${JSON.stringify(data)}`);
  }
}

// ─── Main ───────────────────────────────────────────────────────────────────
async function main() {
  const [siteId, siteUrl] = process.argv.slice(2);
  if (!siteId || !siteUrl) {
    console.error('Usage: node check-metrics.js <siteId> <siteUrl>');
    process.exit(1);
  }

  log(`Site ${siteId} (${siteUrl}) — mesure SSL + temps de chargement...`);

  const [tlsInfo, loadTimeMs] = await Promise.all([getTlsExpiry(siteUrl), getLoadTimeMs(siteUrl)]);

  const metrics = {
    sslValidTo: tlsInfo ? tlsInfo.validTo : null,
    sslDaysRemaining: tlsInfo ? tlsInfo.daysRemaining : null,
    sslIssuer: tlsInfo ? tlsInfo.issuer : null,
    loadTimeMs,
  };

  log('Résultat :', metrics);

  try {
    await sendMetrics(siteId, metrics);
    log('✅ Métriques envoyées au relay.');
  } catch (err) {
    log('❌ Échec envoi au relay :', err.message);
    process.exitCode = 1; // n'interrompt pas le job si appelé avec `|| true`
  }
}

main();
