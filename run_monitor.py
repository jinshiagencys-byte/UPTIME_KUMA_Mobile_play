"""
OpenBrowser-AI — Monitoring fonctionnel (version budget zero : OpenRouter gratuit).

STRATEGIE : openrouter/free (x2) puis Cohere command-a-03-2025 en secours.
Test test_providers.py du 2026-09-19 : seuls OpenRouter et Cohere ont passe les 3 tests.
Groq (limite 8000 tokens/min : prompt de l'agent refuse en 413), Google (quota), DeepSeek /
HF (solde 0), NVIDIA (modele retire) restent DESACTIVES (ENABLE_FALLBACKS=1 pour Groq/Google).

NOTE (2026-09-21) : TypeSafe Jev (typesafe/jev-1.13 / jev-latest) evalue puis ECARTE comme
modele principal — il ne s'utilise pas via /chat/completions mais via l'endpoint OpenRouter
"Decisions", qui renvoie une probabilite par question fermee (yes/no), pas du texte libre.
Incompatible avec CodeAgent qui a besoin de blocs Python generes librement pour naviguer
(click/input_text/etc.). Reste une piste possible plus tard comme validateur de fin de
chaine (juger si le rapport produit est un JSON UP/DOWN valide), pas comme moteur de nav.

Changements de cette version :
  A. Plusieurs pages par run : le workflow doit passer PAGES_JSON (liste des pages
     du relay). SITE_URL est toujours teste ; les autres pages tournent par
     rotation (MAX_PAGES_PER_RUN). Les pages non testees sont rapportees en
     UNKNOWN pour ne pas etre supprimees par pages-report.
  B. Prompt plus strict : format ```python obligatoire (openrouter/free repond
     parfois en <tool_call>...), une action par etape, jamais de done() avant
     d'avoir vu le resultat, budget d'etapes explicite.
  C. MAX_STEPS 8 -> 12 (les reponses hors format consomment des etapes).
  D. Plusieurs essais du meme modele (OPENROUTER_ATTEMPTS) : openrouter/free
     change de modele sous le capot, un 2e essai peut reussir.
  E. Constat partiel conserve : si l'agent a interagi mais n'a pas appele done(),
     on garde un rapport ERROR factuel (pas de verdict UP/DOWN invente) au lieu
     de "Tous les modeles ont echoue".
  F. Detection quota/erreur fatale : plus de correspondance sur "429"/"402" nus.

Acquis conserves : browser=BrowserSession(...) + await start() manuel, consignes
fusionnees dans task, ChatOpenAI pour tous les providers, timeout par appel LLM,
verdict uniquement depuis done(), monkeypatch video (gel des frames).
"""
import asyncio
import glob
import json
import logging
import os
import re
import sys
import threading
import time

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

from openai import AsyncOpenAI
from openbrowser import CodeAgent
from openbrowser.llm import ChatOpenAI
from openbrowser.browser import BrowserProfile, BrowserSession
from openbrowser.browser.video_recorder import VideoRecorderService
from openbrowser.browser.watchdogs.recording_watchdog import RecordingWatchdog


# ---------------------------------------------------------------------------
# MONKEYPATCH VIDEO : combler les trous pendant les attentes LLM
# (Page.startScreencast n'emet une frame que sur repaint ; le recorder ecrit a
# 30 fps fixes -> on rejoue la derniere image pendant les pauses, plafonnee.)
# ---------------------------------------------------------------------------
FREEZE_CAP_SECONDS = float(os.environ.get("VIDEO_FREEZE_CAP_SECONDS", "3.0"))
MIN_GAP_SECONDS = 0.5

_video_lock = threading.Lock()
_orig_recorder_start = VideoRecorderService.start
_orig_recorder_add_frame = VideoRecorderService.add_frame


def _patched_recorder_start(self):
    _orig_recorder_start(self)
    self._last_array = None
    self._last_wall = None
    writer = self._writer
    if writer is not None:
        raw_append = writer.append_data

        def append_and_remember(arr, *args, **kwargs):
            self._last_array = arr
            return raw_append(arr, *args, **kwargs)

        writer.append_data = append_and_remember
        self._raw_append = raw_append


def _patched_recorder_add_frame(self, frame_data_b64, received_at=None):
    now = received_at if received_at is not None else time.monotonic()
    with _video_lock:
        last = getattr(self, "_last_array", None)
        last_t = getattr(self, "_last_wall", None)
        if (
            self._is_active
            and self._writer is not None
            and last is not None
            and last_t is not None
        ):
            gap = now - last_t
            if gap > MIN_GAP_SECONDS:
                n_fill = int(min(gap, FREEZE_CAP_SECONDS) * self.framerate)
                for _ in range(n_fill):
                    self._raw_append(last)
        _orig_recorder_add_frame(self, frame_data_b64)
        self._last_wall = now if last_t is None else max(now, last_t)


def _patched_on_screencast_frame(self, event, session_id):
    """Copie de RecordingWatchdog.on_screencastFrame (openbrowser-ai 0.1.50)
    + horodatage de reception transmis a add_frame."""
    if not self._recorder:
        return
    loop = asyncio.get_running_loop()
    loop.run_in_executor(
        None, self._recorder.add_frame, event["data"], time.monotonic()
    )
    asyncio.create_task(self._ack_screencast_frame(event, session_id))


VideoRecorderService.start = _patched_recorder_start
VideoRecorderService.add_frame = _patched_recorder_add_frame
RecordingWatchdog.on_screencastFrame = _patched_on_screencast_frame


# ---------------------------------------------------------------------------
# Env / reglages
# ---------------------------------------------------------------------------
SITE_URL = os.environ.get("SITE_URL", "")
SITE_ID = os.environ.get("SITE_ID", "")
SITE_TYPE = os.environ.get("SITE_TYPE", "generic")
REQUIREMENTS = os.environ.get("REQUIREMENTS", "")
PAGES_JSON = os.environ.get("PAGES_JSON", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
COHERE_API_KEY = os.environ.get("COHERE_API_KEY", "")
# PUTER retire (2026-09-21) : confirme mort, preflight echoue en 402
# subscription_required sur gemini-2.5-flash-lite -> le tier gratuit de
# Puter ne couvre pas l'acces API cle-en-main, seulement le SDK navigateur.

# Modeles OpenRouter GRATUITS nommes explicitement (au lieu de l'alias
# "openrouter/free" qui route vers un modele cache/changeant -> source
# d'une partie des reponses hors format). Ordre choisi pour un agent de
# navigation/QA : le plus adapte a la tache en premier.
OPENROUTER_FREE_MODELS = os.environ.get(
    "OPENROUTER_FREE_MODELS",
    "nex-agi/nex-n2.5-mini,z-ai/glm-5.2,qwen/qwen3.8-27b,"
    "poolside/laguna-xs-2.1,nvidia/nemotron-3-super"
)

ENABLE_FALLBACKS = os.environ.get("ENABLE_FALLBACKS", "0") == "1"
OPENROUTER_ATTEMPTS = int(os.environ.get("OPENROUTER_ATTEMPTS", "1"))
COHERE_ATTEMPTS = int(os.environ.get("COHERE_ATTEMPTS", "1"))
DEAD_PROVIDERS = set()  # (provider, modele) abandonnes pour ce run (quota / erreur fatale)
MAX_PAGES_PER_RUN = int(os.environ.get("MAX_PAGES_PER_RUN", "2"))

LLM_CALL_TIMEOUT = float(os.environ.get("LLM_CALL_TIMEOUT_SECONDS", "100"))
GLOBAL_TIMEOUT_SECONDS = float(os.environ.get("GLOBAL_TIMEOUT_SECONDS", "360"))
TOTAL_BUDGET_SECONDS = float(os.environ.get("TOTAL_BUDGET_SECONDS", "720"))
MIN_ATTEMPT_SECONDS = 120  # ne pas demarrer un essai s'il reste moins que ca

MAX_STEPS = int(os.environ.get("MAX_STEPS", "12"))
TEST_STEPS = max(MAX_STEPS - 4, 4)   # etapes de test
DONE_STEP = TEST_STEPS + 1           # a partir d'ici : done() obligatoire
DELAY_BETWEEN_ATTEMPTS = 3

RECORDINGS_DIR = os.path.abspath("./recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

print("Recordings dir : " + RECORDINGS_DIR)
print("Groq key          : " + str(bool(GROQ_API_KEY)))
print("Google AI Std key : " + str(bool(GOOGLE_API_KEY)))
print("OpenRouter key    : " + str(bool(OPENROUTER_API_KEY)))
print("Cohere key        : " + str(bool(COHERE_API_KEY)))
print("Fallbacks payants/quota : " + ("ON" if ENABLE_FALLBACKS else "OFF"))
print("Video freeze cap  : %.1fs" % FREEZE_CAP_SECONDS)
print("Max steps / essai : %d | pages max / run : %d" % (MAX_STEPS, MAX_PAGES_PER_RUN))


# ---------------------------------------------------------------------------
# LLM — ChatOpenAI pour TOUS les providers
# ---------------------------------------------------------------------------
def build_llm(model_config):
    llm_kwargs = {}
    if model_config["provider"] != "openrouter":
        llm_kwargs = {"frequency_penalty": None, "max_completion_tokens": None}
    return ChatOpenAI(
        model=model_config["model"],
        base_url=model_config["base_url"],
        api_key=model_config["key"],
        temperature=0.0,
        timeout=LLM_CALL_TIMEOUT,
        max_retries=0,
        **llm_kwargs,
    )


# ---------------------------------------------------------------------------
# Chaine de modeles : OpenRouter (x OPENROUTER_ATTEMPTS) puis, si active, le reste
# ---------------------------------------------------------------------------
MODEL_CHAIN = []

if OPENROUTER_API_KEY:
    free_models = [m.strip() for m in OPENROUTER_FREE_MODELS.split(",") if m.strip()]
    for model_name in free_models:
        for _ in range(max(OPENROUTER_ATTEMPTS, 1)):
            MODEL_CHAIN.append({
                "provider": "openrouter",
                "model": model_name,
                "key": OPENROUTER_API_KEY,
                "base_url": "https://openrouter.ai/api/v1",
            })

if COHERE_API_KEY:
    for _ in range(max(COHERE_ATTEMPTS, 1)):
        MODEL_CHAIN.append({
            "provider": "cohere",
            "model": os.environ.get("COHERE_MODEL", "command-a-03-2025"),
            "key": COHERE_API_KEY,
            "base_url": "https://api.cohere.ai/compatibility/v1",
        })

if ENABLE_FALLBACKS:
    GROQ_MODELS = os.environ.get("GROQ_MODELS", "openai/gpt-oss-120b")
    GOOGLE_MODELS = os.environ.get("GOOGLE_MODELS", "gemini-3.5-flash")
    if GROQ_API_KEY:
        for m in [x.strip() for x in GROQ_MODELS.split(",") if x.strip()]:
            MODEL_CHAIN.append({
                "provider": "groq", "model": m, "key": GROQ_API_KEY,
                "base_url": "https://api.groq.com/openai/v1",
            })
    if GOOGLE_API_KEY:
        for m in [x.strip() for x in GOOGLE_MODELS.split(",") if x.strip()]:
            MODEL_CHAIN.append({
                "provider": "google_openai", "model": m, "key": GOOGLE_API_KEY,
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            })

if not MODEL_CHAIN:
    print("ERREUR : aucune cle API disponible. Abandon.")
    sys.exit(1)

print("Chaine finale :")
for i, entry in enumerate(MODEL_CHAIN):
    print("  %d. [%s] %s" % (i + 1, entry["provider"], entry["model"]))


# ---------------------------------------------------------------------------
# Consignes (fusionnees dans task : CodeAgent n'a pas de system prompt custom)
# ---------------------------------------------------------------------------
CONSIGNES_TEMPLATE = """
Tu es un ingenieur QA fonctionnel. TESTE l'application web, ne te contente pas d'observer.

FORMAT DE SORTIE (CRITIQUE : une reponse hors format fait PERDRE une etape) :
- Chaque reponse = UNE phrase courte, puis UN SEUL bloc ```python.
- JAMAIS de tags <tool_call>, <arg_key>, <arg_value>. JAMAIS de nom_outil(args) hors bloc python.
- UNE seule petite action par etape (navigate, OU input_text puis click, OU un scroll).
- N'ecris JAMAIS await done() dans la meme reponse que tes tests. Attends d'avoir
  VU le resultat de tes actions (etape suivante) avant de conclure.
- N'invente PAS de selecteurs CSS : utilise les index des elements affiches dans
  l'etat de la page (click(index=N), input_text(index=N, text=...)).

Exemple de reponse valide :
Je verifie le titre de la page.
```python
title = await evaluate('document.title')
print(title)
```

BUDGET : __MAX_STEPS__ etapes au total.
- Etapes 1 a __TEST_STEPS__ : tester.
- A partir de l'etape __DONE_STEP__ : appelle done() avec ce que tu as observe, meme incomplet.
- Un rapport incomplet mais honnete vaut mieux que pas de rapport.

REGLES DE TEST :
1. Effectue au moins UNE vraie interaction utilisateur (click / input_text / scroll) et verifie le resultat.
2. Page chargee N'EST PAS un test. Bouton existe N'EST PAS un test.
3. Test valide = action + observation + assertion.
4. Ne declare UP que si tu as vu de tes yeux le resultat attendu apres ton interaction.

ANOMALIES (echecs fonctionnels) :
- Texte contenant Erreur, Error, Failed, undefined, null, 0 produit, Aucun produit.
- Lien avec /undefined dans l'URL. Image avec alt="undefined".
- Liste de produits vide sur une page catalogue. Carrousel/composant en erreur de chargement.

PAGE A TESTER : __TARGET_URL__
INTERACTIONS REQUISES :
__REQUIREMENTS__

SORTIE FINALE :
Appelle done(text=...) avec UNIQUEMENT un JSON valide dans text :
  overall_status (UP ou DOWN), site_type, actions_completed, model_used, pages[].

Exemple de forme (remplace par le resultat REEL observe) :
```python
import json
result = {"overall_status": "DOWN ou UP selon ce que tu as observe",
"site_type": "__SITE_TYPE__", "actions_completed": True,
"model_used": "__MODEL_NAME__",
"pages": [{"url": "__TARGET_URL__", "status": "DOWN ou UP",
"http_code": 200, "action_tested": "description de l'action",
"assertion_passed": True_ou_False, "note": "ce que tu as observe"}]}
await done(text=json.dumps(result, ensure_ascii=False), success=True)
```
"""

# Pour les pages secondaires (le REQUIREMENTS du site vise la page d'accueil)
PAGE_REQUIREMENTS = (
    "1. Assert the page content loaded (title and main content are not an error, "
    "placeholder or empty). 2. Perform ONE real interaction (click a link/button, "
    "or scroll and click something) and assert the result is correct."
)


def build_full_task(model_name, target_url, requirements):
    basic_task = (
        "Navigate to " + target_url + ". "
        "Execute the required interactions. Use Python assertions. "
        "Detect anomalies (errors, undefined, empty listings). "
        "IMPORTANT: write ONLY valid Python code blocks between triple backticks."
    )
    consignes = (
        CONSIGNES_TEMPLATE
        .replace("__REQUIREMENTS__", requirements)
        .replace("__SITE_TYPE__", SITE_TYPE)
        .replace("__MODEL_NAME__", model_name)
        .replace("__TARGET_URL__", target_url)
        .replace("__MAX_STEPS__", str(MAX_STEPS))
        .replace("__TEST_STEPS__", str(TEST_STEPS))
        .replace("__DONE_STEP__", str(DONE_STEP))
    )
    return basic_task + "\n\n---\n\n" + consignes


# ---------------------------------------------------------------------------
# JSON / rapport (le verdict vient UNIQUEMENT de done())
# ---------------------------------------------------------------------------
def extract_json_from_text(text):
    if not text:
        return None
    match = re.search(r"{.*}", text, re.DOTALL)
    if not match:
        return None
    candidate = match.group(0)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        try:
            cleaned = re.sub(r"(?<!\\)\n", " ", candidate)
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


def extract_final_result(result):
    cells = getattr(result, "cells", None) or getattr(result, "history", None) or []
    for cell in reversed(list(cells)):
        source = getattr(cell, "source", "") or ""
        if "done(" not in source:
            continue
        match = re.search(
            r"done\s*\(\s*text\s*=\s*['\"](.+?)['\"]\s*,\s*success",
            source, re.DOTALL,
        )
        if match:
            return match.group(1)
    for cell in reversed(list(cells)):
        output = getattr(cell, "output", "") or ""
        if '"url"' in output or '"overall_status"' in output:
            return output
    return ""  # surtout PAS str(result) : il contient toujours "error=None"


VALID_STATUSES = ("UP", "DOWN", "ERROR", "UNKNOWN")

_INTERACTION_RE = re.compile("|".join([
    r"\b(?:click|input_text|send_keys|select_dropdown|scroll|upload_file)\s*\(",
    r"\.(?:click|focus|submit)\s*\(",
    r"\bscroll(?:To|By|IntoView)\b",
    r"\bdispatchEvent\b",
]))


def parse_report(text):
    if not text:
        return None
    candidate = None
    if isinstance(text, dict):
        candidate = text
    else:
        text = str(text).strip()
        try:
            candidate = json.loads(text)
        except json.JSONDecodeError:
            candidate = extract_json_from_text(text)
    if (
        isinstance(candidate, dict)
        and str(candidate.get("overall_status", "")).upper() in VALID_STATUSES
    ):
        return candidate
    return None


def get_agent_report(agent, result):
    ns = getattr(agent, "namespace", None) or {}
    if ns.get("_task_done") and ns.get("_task_result"):
        report = parse_report(ns["_task_result"])
        if report is not None:
            return report
    return parse_report(extract_final_result(result))


def count_real_interactions(cells):
    n = 0
    for cell in cells:
        if getattr(cell, "error", None):
            continue
        source = getattr(cell, "source", "") or ""
        if "done(" in source:
            continue
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        if _INTERACTION_RE.search(code):
            n += 1
    return n


def normalize_report(report, model_name, target_url):
    status = str(report["overall_status"]).upper()
    report["overall_status"] = status
    report["model_used"] = model_name
    report.setdefault("site_type", SITE_TYPE)
    report.setdefault("actions_completed", True)
    if not isinstance(report.get("pages"), list) or not report["pages"]:
        report["pages"] = [{
            "url": target_url,
            "status": status,
            "http_code": None,
            "action_tested": None,
            "assertion_passed": status == "UP",
            "note": "Rapport sans detail par page.",
        }]
    return report


def judge_run(agent, result, model_name, target_url):
    cells = list(getattr(result, "cells", None) or [])
    report = get_agent_report(agent, result)
    interactions = count_real_interactions(cells)
    if report is None:
        return None, "aucun rapport JSON remis par done() : l'agent n'a pas conclu"
    if str(report["overall_status"]).upper() == "UP" and interactions == 0:
        return None, "UP annonce sans aucune interaction reelle : pas un test"
    return normalize_report(report, model_name, target_url), \
        "%d interaction(s) reelle(s)" % interactions


def collapse_to_page(report, target_url):
    """Un run = une entree de page, sur l'URL testee (URLs stables pour le relay :
    pages-report supprime/recree les URLs absentes du rapport)."""
    pages = [p for p in (report.get("pages") or []) if isinstance(p, dict)]
    status = str(report["overall_status"]).upper()
    actions = [str(p["action_tested"]) for p in pages if p.get("action_tested")]
    notes = [str(p["note"]) for p in pages if p.get("note")]
    codes = [p["http_code"] for p in pages if p.get("http_code")]
    if pages:
        passed = all(bool(p.get("assertion_passed")) for p in pages)
    else:
        passed = status == "UP"
    return {
        "url": target_url,
        "status": status,
        "http_code": codes[0] if codes else None,
        "action_tested": (" ; ".join(actions))[:300] or None,
        "assertion_passed": passed,
        "note": (" | ".join(notes))[:400],
    }


def build_partial_report(cells, model_name, target_url):
    """Constat partiel FACTUEL quand l'agent n'a pas conclu. Jamais UP/DOWN :
    uniquement ERROR + ce qui a ete execute/observe. Retourne (page, score)."""
    interactions = count_real_interactions(cells)
    if interactions == 0:
        return None, 0
    obs = []
    for cell in cells[-4:]:
        err = getattr(cell, "error", None)
        out = (getattr(cell, "output", "") or "").strip()
        if err:
            obs.append("erreur: " + str(err)[:150])
        elif out:
            obs.append(out[:150])
    note = "Test incomplet (l'agent n'a pas remis de rapport done()). %d interaction(s) executee(s)." % interactions
    if obs:
        note += " Dernieres observations : " + " / ".join(obs)
    return {
        "url": target_url,
        "status": "ERROR",
        "http_code": None,
        "action_tested": "%d interaction(s) sans conclusion (%s)" % (interactions, model_name),
        "assertion_passed": False,
        "note": note[:500],
    }, interactions


# ---------------------------------------------------------------------------
# Fermeture session (flush video via BrowserStopEvent interne)
# ---------------------------------------------------------------------------
async def close_agent_session(agent):
    if agent is None:
        return
    session = None
    for attr_name in ("browser_session", "browser", "session"):
        session = getattr(agent, attr_name, None)
        if session is not None:
            print("Session trouvee via agent." + attr_name)
            break
    if session is None:
        print("Aucune session trouvee sur l'agent")
    else:
        for method_name in ("close", "stop", "kill", "shutdown", "cleanup"):
            if not hasattr(session, method_name):
                continue
            try:
                r = getattr(session, method_name)()
                if asyncio.iscoroutine(r):
                    await r
                print("Session fermee via " + method_name + "()")
                break
            except Exception as e:
                print("Echec " + method_name + "() : " + str(e))

    await asyncio.sleep(5)
    videos = (
        glob.glob(os.path.join(RECORDINGS_DIR, "*.mp4"))
        + glob.glob(os.path.join(RECORDINGS_DIR, "*.webm"))
    )
    print("Videos dans %s : %d" % (RECORDINGS_DIR, len(videos)))
    for v in videos:
        print("  -> " + v + " (" + str(os.path.getsize(v)) + " octets)")


# ---------------------------------------------------------------------------
# Detection erreurs fatales / quota (phrases precises, PAS de codes nus "429"/"402")
# ---------------------------------------------------------------------------
def is_fatal_model_error(text):
    lower = text.lower()
    return any(p in lower for p in [
        "agentic harness",
        "only available on agentic",
        "no endpoints found",
        "this model is unavailable",
        "model not found",
        "is no longer available",
        "unavailable for free",
        "authenticationerror",
        "invalid_api_key",
        "not found for account",
    ])


def is_quota_error(text):
    lower = text.lower()
    return any(p in lower for p in [
        "quota exceeded",
        "resource_exhausted",
        "too many requests",
        "rate limit exceeded",
        "free-models-per-day",
        "free_tier_requests",
        "insufficient balance",
        "insufficient_balance",
        "balance is not enough",
    ])


# ---------------------------------------------------------------------------
# Preflight (1 token), mis en cache par modele
# ---------------------------------------------------------------------------
_preflight_cache = {}


async def preflight_api_check(model_config):
    key = (model_config["provider"], model_config["model"])
    if key in _preflight_cache:
        return _preflight_cache[key]
    try:
        client = AsyncOpenAI(
            api_key=model_config["key"],
            base_url=model_config["base_url"],
        )
        await asyncio.wait_for(
            client.chat.completions.create(
                model=model_config["model"],
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
            ),
            timeout=20,
        )
        print("Preflight OK : %s" % model_config["model"])
        ok = True
    except Exception as e:
        msg = str(e)
        print("Preflight ECHEC : %s -> %s" % (model_config["model"], msg[:200]))
        ok = False
    _preflight_cache[key] = ok
    return ok


# ---------------------------------------------------------------------------
# Une tentative sur UNE page. Retourne un dict :
#   report  : rapport valide de done() ou None
#   partial : (page_dict, score) constat partiel ou (None, 0)
#   stop    : True si quota/erreur fatale -> inutile de continuer
# ---------------------------------------------------------------------------
async def run_attempt(model_config, target_url, requirements):
    provider = model_config["provider"]
    model_name = model_config["model"]

    print("=" * 60)
    print("TENTATIVE - PROVIDER : %s | MODELE : %s | PAGE : %s" % (
        provider, model_name, target_url))
    print("=" * 60)

    outcome = {"report": None, "partial": (None, 0), "stop": False}
    agent = None
    browser_session = None
    start_time = time.time()

    try:
        llm = build_llm(model_config)
        profile = BrowserProfile(
            headless=True,
            viewport_width=1280,
            viewport_height=720,
            record_video_dir=RECORDINGS_DIR,
        )
        browser_session = BrowserSession(browser_profile=profile)
        await browser_session.start()  # CodeAgent ne le fait pas pour une session fournie

        full_task = build_full_task(model_name, target_url, requirements)
        agent = CodeAgent(
            task=full_task,
            llm=llm,
            browser=browser_session,
            max_steps=MAX_STEPS,
        )

        try:
            result = await asyncio.wait_for(
                agent.run(), timeout=GLOBAL_TIMEOUT_SECONDS)
            print("agent.run() termine en %.1fs" % (time.time() - start_time))
        except asyncio.TimeoutError:
            print("Timeout global pour " + model_name)
            return outcome

        cells = list(getattr(result, "cells", None) or [])
        report, reason = judge_run(agent, result, model_name, target_url)
        print("Cellules executees : %d | %s" % (len(cells), reason))

        if report is not None:
            print("Rapport retenu (" + model_name + ") :")
            print(json.dumps(report, ensure_ascii=False)[:800])
            outcome["report"] = report
            return outcome

        outcome["partial"] = build_partial_report(cells, model_name, target_url)
        raw = str(result)
        if is_fatal_model_error(raw):
            print("ERREUR FATALE : %s -> stop" % model_name)
            outcome["stop"] = True
        elif is_quota_error(raw):
            print("QUOTA/SOLDE : %s -> stop" % model_name)
            outcome["stop"] = True
        else:
            print("REJETE : %s -> %s" % (model_name, reason))
        return outcome

    except Exception as err:
        text = str(err)
        print("Echec avec " + model_name + " : " + text)
        if is_fatal_model_error(text) or is_quota_error(text):
            outcome["stop"] = True
        return outcome

    finally:
        await close_agent_session(agent)


# ---------------------------------------------------------------------------
# Test d'une page : parcourt la chaine (essais successifs), garde le meilleur partiel
# ---------------------------------------------------------------------------
async def test_page(target_url, requirements, deadline):
    best_partial = (None, 0)
    for idx, model_config in enumerate(MODEL_CHAIN):
        dead_key = (model_config["provider"], model_config["model"])
        if dead_key in DEAD_PROVIDERS:
            continue
        if time.monotonic() > deadline - MIN_ATTEMPT_SECONDS:
            print("Budget de temps epuise : plus d'essai pour %s" % target_url)
            break
        print("")
        print("#" * 60)
        print("# %s -- essai %d/%d : [%s] %s" % (
            target_url, idx + 1, len(MODEL_CHAIN),
            model_config["provider"], model_config["model"]))
        print("#" * 60)

        if not await preflight_api_check(model_config):
            continue

        outcome = await run_attempt(model_config, target_url, requirements)
        if outcome["report"] is not None:
            return outcome["report"], best_partial, False
        if outcome["partial"][1] > best_partial[1]:
            best_partial = outcome["partial"]
        if outcome["stop"]:
            DEAD_PROVIDERS.add(dead_key)
            print("Modele abandonne pour ce run : [%s] %s" % dead_key)
            continue
        if idx < len(MODEL_CHAIN) - 1:
            print("Attente %ds..." % DELAY_BETWEEN_ATTEMPTS)
            await asyncio.sleep(DELAY_BETWEEN_ATTEMPTS)
    all_dead = all((mc["provider"], mc["model"]) in DEAD_PROVIDERS for mc in MODEL_CHAIN)
    return None, best_partial, all_dead


# ---------------------------------------------------------------------------
# Selection des pages : SITE_URL toujours, les autres par rotation (2 runs/jour)
# ---------------------------------------------------------------------------
def load_other_pages():
    others, seen = [], {SITE_URL.rstrip("/")}
    if not PAGES_JSON.strip():
        return others
    try:
        data = json.loads(PAGES_JSON)
    except json.JSONDecodeError as e:
        print("PAGES_JSON invalide (%s) : ignore" % e)
        return others
    for p in data or []:
        url = (p.get("url") if isinstance(p, dict) else str(p)) or ""
        url = url.strip()
        key = url.rstrip("/")
        if url.startswith("http") and key not in seen:
            seen.add(key)
            others.append(url)
    return others


def select_pages():
    others = load_other_pages()
    slots = max(MAX_PAGES_PER_RUN - 1, 0)
    if not others or slots == 0:
        return [SITE_URL], others
    slot_index = int(time.time() // 43200)  # change toutes les 12 h
    start = (slot_index * slots) % len(others)
    picked = [others[(start + i) % len(others)] for i in range(min(slots, len(others)))]
    untested = [u for u in others if u not in picked]
    return [SITE_URL] + picked, untested


def merge_status(statuses):
    if "DOWN" in statuses:
        return "DOWN"
    if "ERROR" in statuses:
        return "ERROR"
    if statuses and all(s == "UP" for s in statuses):
        return "UP"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    t0 = time.monotonic()
    deadline = t0 + TOTAL_BUDGET_SECONDS

    targets, untested = select_pages()
    print("")
    print("Pages a tester ce run : " + ", ".join(targets))
    if untested:
        print("Pages non testees (rapportees UNKNOWN) : " + ", ".join(untested))

    page_entries = []
    tested_statuses = []
    models_used = []
    completed_any = False
    stopped = False

    for i, url in enumerate(targets):
        if stopped:
            page_entries.append({
                "url": url, "status": "UNKNOWN", "http_code": None,
                "action_tested": None, "assertion_passed": False,
                "note": "Non testee : quota ou erreur fatale sur tous les fournisseurs.",
            })
            continue

        requirements = REQUIREMENTS if i == 0 else PAGE_REQUIREMENTS
        report, partial, stop = await test_page(url, requirements, deadline)
        stopped = stopped or stop

        if report is not None:
            entry = collapse_to_page(report, url)
            models_used.append(report.get("model_used"))
            completed_any = True
        elif partial[0] is not None:
            entry = partial[0]
        else:
            entry = {
                "url": url, "status": "ERROR", "http_code": None,
                "action_tested": None, "assertion_passed": False,
                "note": "Aucun test exploitable : tous les essais ont echoue.",
            }
        page_entries.append(entry)
        tested_statuses.append(entry["status"])

    for url in untested:
        page_entries.append({
            "url": url, "status": "UNKNOWN", "http_code": None,
            "action_tested": None, "assertion_passed": False,
            "note": "Non testee ce run (rotation) : conservee pour ne pas etre supprimee.",
        })

    final_report = {
        "overall_status": merge_status(tested_statuses),
        "site_type": SITE_TYPE,
        "actions_completed": completed_any,
        "model_used": models_used[-1] if models_used else None,
        "pages": page_entries,
    }
    if not completed_any:
        final_report["error"] = "Aucun rapport complet remis par l'agent."

    output_path = os.path.join(os.getcwd(), "output.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final_report, f, indent=2, ensure_ascii=False)

    print("")
    print("Duree totale : %.0fs" % (time.monotonic() - t0))
    print("=== output.json ===")
    print(json.dumps(final_report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
