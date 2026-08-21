const { chromium } = require('playwright');

const RELAY_URL = process.env.RELAY_URL;
const RELAY_SECRET = process.env.RELAY_SECRET;
const KUMA_URL = process.env.KUMA_URL;

if (!RELAY_URL || !RELAY_SECRET || !KUMA_URL) {
  console.error('RELAY_URL, RELAY_SECRET et KUMA_URL sont requis (env vars).');
  process.exit(1);
}

async function relayGet(path) {
  const res = await fetch(`${RELAY_URL}${path}`, {
    headers: { 'x-relay-secret': RELAY_SECRET },
  });
  const data = await res.json();
  if (!res.ok || data?.success !== true) {
    throw new Error(`Relay error on ${path}: ${data?.error || res.status}`);
  }
  return data;
}

async function pushStatus(pushToken, status, msg) {
  const url = `${KUMA_URL}/api/push/${pushToken}?status=${status}&msg=${encodeURIComponent(msg)}&ping=`;
  try {
    const res = await fetch(url);
    if (!res.ok) {
      console.error(`[push] échec HTTP ${res.status} pour token ${pushToken.slice(0, 6)}...`);
    }
  } catch (e) {
    console.error(`[push] erreur réseau pour token ${pushToken.slice(0, 6)}...:`, e.message);
  }
}

// Vérifie une page : navigation OK + pas d'erreur API silencieuse en arrière-plan
// (c'est le but même du projet : détecter un backend qui répond en erreur
// alors que le frontend a l'air fonctionnel).
async function checkPage(browser, url) {
  const context = await browser.newContext();
  const page = await context.newPage();

  const apiErrors = [];
  const consoleErrors = [];

  page.on('response', (response) => {
    const req = response.request();
    const type = req.resourceType();
    // On ne surveille que les appels XHR/fetch (appels API), pas les assets
    // statiques (images, css, fonts) qui peuvent 404 sans que ce soit critique.
    if ((type === 'xhr' || type === 'fetch') && response.status() >= 500) {
      apiErrors.push(`${response.status()} ${req.url()}`);
    }
  });

  page.on('pageerror', (err) => {
    consoleErrors.push(err.message);
  });

  let status = 'up';
  let msg = 'OK';

  try {
    const response = await page.goto(url, { waitUntil: 'networkidle', timeout: 20000 });

    if (!response || !response.ok()) {
      status = 'down';
      msg = `HTTP ${response ? response.status() : 'no response'}`;
    } else if (apiErrors.length > 0) {
      status = 'down';
      msg = `Erreur API: ${apiErrors[0]}`;
    } else if (consoleErrors.length > 0) {
      status = 'down';
      msg = `Erreur JS: ${consoleErrors[0]}`;
    }
  } catch (err) {
    status = 'down';
    msg = String(err.message || err).slice(0, 200);
  } finally {
    await context.close();
  }

  return { status, msg };
}

async function main() {
  const { sites } = await relayGet('/active-sites');
  console.log(`[crawler] ${sites.length} site(s) actif(s) à vérifier`);

  const browser = await chromium.launch();
  let totalPages = 0;
  let totalDown = 0;

  try {
    for (const site of sites) {
      if (!site.kuma_group_id) {
        console.log(`[crawler] site "${site.client_name}" sans kuma_group_id, ignoré`);
        continue;
      }

      let tokens;
      try {
        const data = await relayGet(`/push-tokens?groupId=${site.kuma_group_id}`);
        tokens = data.tokens;
      } catch (e) {
        console.error(`[crawler] impossible de récupérer les tokens pour "${site.client_name}":`, e.message);
        continue;
      }

      console.log(`[crawler] "${site.client_name}": ${tokens.length} page(s) à vérifier`);

      for (const token of tokens) {
        totalPages += 1;
        const { status, msg } = await checkPage(browser, token.url);
        if (status === 'down') totalDown += 1;
        console.log(`[crawler]   ${token.url} -> ${status} (${msg})`);
        await pushStatus(token.push_token, status, msg);
      }
    }
  } finally {
    await browser.close();
  }

  console.log(`[crawler] terminé : ${totalPages} page(s) vérifiée(s), ${totalDown} en down.`);
}

main().catch((err) => {
  console.error('[crawler] erreur fatale:', err);
  process.exit(1);
});
