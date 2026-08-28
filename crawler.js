const EXPECTED_SELECTOR = process.env.EXPECTED_SELECTOR || ''; // ex: ".realisation-item"
const EXPECTED_TEXT = process.env.EXPECTED_TEXT || ''; // optionnel
const WAIT_TIMEOUT_MS = parseInt(process.env.WAIT_TIMEOUT_MS || '10000', 10);

async function checkPage(context, url) {
  const page = await context.newPage();
  let status = 'up';
  let msg = 'OK';
  let loadTimeMs = null;
  const navStart = Date.now();

  try {
    const response = await page.goto(url, { waitUntil: 'load', timeout: 20000 });

    if (!response || !response.ok()) {
      status = 'down';
      msg = `HTTP ${response ? response.status() : 'no response'}`;
    } else {
      // On attend que le contenu backend apparaisse
      if (EXPECTED_SELECTOR) {
        try {
          if (EXPECTED_TEXT) {
            // Attendre un élément contenant le texte exact
            await page.waitForSelector(`${EXPECTED_SELECTOR}:has-text("${EXPECTED_TEXT}")`, { timeout: WAIT_TIMEOUT_MS });
          } else {
            // Attendre simplement que l'élément existe et soit visible
            await page.waitForSelector(EXPECTED_SELECTOR, { state: 'visible', timeout: WAIT_TIMEOUT_MS });
          }
        } catch (e) {
          status = 'down';
          msg = `Contenu backend non affiché après ${WAIT_TIMEOUT_MS}ms (${EXPECTED_SELECTOR})`;
        }
      } else {
        console.warn('[crawler] Aucun EXPECTED_SELECTOR défini, on considère juste le chargement HTTP.');
      }
    }

    // Temps de chargement
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
