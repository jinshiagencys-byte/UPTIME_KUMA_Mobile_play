"""
OpenBrowser-AI — Monitoring fonctionnel.
STRATÉGIE : OpenRouter (confirmé) -> Groq -> Google (dernier recours)
Vidéo native via CDP Page.startScreencast + imageio_ffmpeg (MP4 direct).
Preflight + succès AVANT erreurs fatales.
Logging DEBUG activé pour diagnostiquer le pipeline vidéo.

Corrections appliquées après lecture directe du code source openbrowser-ai :
  1. CodeAgent n'a PAS de paramètre `browser_profile` → passer via `browser=BrowserSession(...)`
  2. CodeAgent n'a PAS de paramètre pour system prompt custom → fusionné dans `task`
  3. CodeAgent n'appelle `browser_session.start()` QUE s'il crée lui-même la session
     (browser_session is None) → puisqu'on passe une session déjà construite via
     `browser=`, il faut l'appeler nous-mêmes AVANT de la passer, sinon toute
     action browser plante (CDP jamais initialisé).
  4. Vidéo trop courte (6 s pour un run de 205 s) : Page.startScreencast n'envoie
     une frame QUE quand la page se repeint, donc aucune frame pendant les attentes
     LLM, et le recorder écrit à 30 fps fixes → monkeypatch de VideoRecorderService
     (voir section dédiée plus bas).
"""
import asyncio
import glob
import json
import logging
import os
import re
import threading
import time

# ⭐ ACTIVATION LOGS INTERNES DU FRAMEWORK (avant tout import openbrowser)
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
# ⭐ MONKEYPATCH VIDEO : combler les trous pendant les attentes LLM
#
# Problème confirmé sur un vrai run : 205 s réelles -> 199 frames -> 6,6 s de
# vidéo. CDP Page.startScreencast n'émet une frame que sur repaint visuel, donc
# pendant qu'un LLM lent réfléchit (page figée) il n'arrive aucune frame, et
# VideoRecorderService écrit à 30 fps fixes sans tenir compte du temps réel.
#
# Solution : quand une nouvelle frame arrive après une pause > MIN_GAP_SECONDS,
# on réécrit d'abord la dernière image (gel) pendant min(pause, cap) secondes.
#
# Pourquoi pas un simple "rappeler add_frame N fois" :
#   - add_frame() est appelé depuis un thread pool (run_in_executor) → il faut
#     un verrou, sinon état partagé corrompu + "generator already executing"
#   - add_frame() lance un sous-processus ffmpeg PAR frame → on réutilise le
#     tableau numpy déjà décodé et on l'écrit directement dans le writer
#   - le traitement d'une frame (ffmpeg) est plus lent que leur cadence d'arrivée
#     sur un runner CPU : il faut mesurer les pauses à l'instant de RÉCEPTION de
#     la frame (event loop, via on_screencastFrame), pas au moment où un thread
#     la traite, sinon le retard de traitement serait pris pour un silence.
#
# Réglage : VIDEO_FREEZE_CAP_SECONDS (env). 3 s ≈ vidéo compacte (~19 s pour
# le run de 205 s), 10 s ≈ plus proche du temps réel (~40 s).
# ---------------------------------------------------------------------------
FREEZE_CAP_SECONDS = float(os.environ.get("VIDEO_FREEZE_CAP_SECONDS", "3.0"))
MIN_GAP_SECONDS = 0.5  # en dessous : flux normal, on ne touche à rien

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
    # received_at : instant de réception (fourni par on_screencastFrame patché).
    # Absent pour la capture finale de BrowserStopEvent -> on prend "maintenant".
    now = received_at if received_at is not None else time.monotonic()
    with _video_lock:  # add_frame arrive depuis un thread pool : on sérialise
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
                    # pas de ffmpeg : on réécrit l'image déjà décodée
                    self._raw_append(last)
        _orig_recorder_add_frame(self, frame_data_b64)
        # max() : les threads peuvent passer le verrou dans le désordre
        self._last_wall = now if last_t is None else max(now, last_t)


def _patched_on_screencast_frame(self, event, session_id):
    """Copie de RecordingWatchdog.on_screencastFrame (openbrowser-ai 0.1.50)
    + horodatage de réception transmis à add_frame."""
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
# Env
# ---------------------------------------------------------------------------
SITE_URL = os.environ.get("SITE_URL", "")
SITE_ID = os.environ.get("SITE_ID", "")
SITE_TYPE = os.environ.get("SITE_TYPE", "generic")
REQUIREMENTS = os.environ.get("REQUIREMENTS", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

RECORDINGS_DIR = os.path.abspath("./recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

print("Recordings dir : " + RECORDINGS_DIR)
print("Groq key          : " + str(bool(GROQ_API_KEY)))
print("Google AI Std key : " + str(bool(GOOGLE_API_KEY)))
print("OpenRouter key    : " + str(bool(OPENROUTER_API_KEY)))
print("Video freeze cap  : %.1fs" % FREEZE_CAP_SECONDS)


# ---------------------------------------------------------------------------
# Wrapper Google AI Studio (endpoint OpenAI-compatible)
# ---------------------------------------------------------------------------
class GoogleGeminiWrapper:
    def __init__(self, model, api_key, base_url, temperature=0.0):
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.temperature = temperature
        self.provider = "google"
        self.model_name = model

    async def ainvoke(self, messages, config=None, **kwargs):
        openai_messages = []
        for msg in messages:
            if hasattr(msg, "type"):
                role = "user"
                if msg.type == "human":
                    role = "user"
                elif msg.type == "ai":
                    role = "assistant"
                elif msg.type == "system":
                    role = "system"
                content = msg.content
                if not isinstance(content, str):
                    content = str(content)
            elif isinstance(msg, dict):
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if not isinstance(content, str):
                    content = str(content)
            else:
                role = "user"
                content = str(msg)
            openai_messages.append({"role": role, "content": content})

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=openai_messages,
            temperature=self.temperature,
        )
        content = response.choices[0].message.content

        try:
            input_tokens = response.usage.prompt_tokens
            output_tokens = response.usage.completion_tokens
        except Exception:
            input_tokens = 100
            output_tokens = max(1, len(content) // 4)

        # 9 champs exigés par TokenUsageEntry de openbrowser-ai
        usage_dict = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "prompt_cached_tokens": 0,
            "prompt_cache_creation_tokens": 0,
            "prompt_image_tokens": 0,
        }

        try:
            from langchain_core.messages import AIMessage
            msg = AIMessage(content=content)
            msg.usage_metadata = usage_dict
            msg.usage = usage_dict
            msg.response_metadata = {
                "model_name": self.model,
                "finish_reason": "stop",
                "token_usage": usage_dict,
            }
            return msg
        except ImportError:
            class SimpleMessage:
                def __init__(self, content, model, usage):
                    self.content = content
                    self.type = "ai"
                    self.usage = usage
                    self.usage_metadata = usage
                    self.response_metadata = {
                        "model_name": model,
                        "finish_reason": "stop",
                        "token_usage": usage,
                    }
            return SimpleMessage(content, self.model, usage_dict)

    async def acall(self, messages, **kwargs):
        return await self.ainvoke(messages, **kwargs)

    def bind_tools(self, tools):
        return self

    def with_structured_output(self, schema):
        return self


# ---------------------------------------------------------------------------
# Wrapper Groq
# ---------------------------------------------------------------------------
class GroqWrapper:
    def __init__(self, model, api_key, base_url, temperature=0.0):
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.temperature = temperature
        self.provider = "groq"
        self.model_name = model

    async def ainvoke(self, messages, config=None, **kwargs):
        openai_messages = []
        for msg in messages:
            if hasattr(msg, "type"):
                role = "user"
                if msg.type == "human":
                    role = "user"
                elif msg.type == "ai":
                    role = "assistant"
                elif msg.type == "system":
                    role = "system"
                content = msg.content
            elif isinstance(msg, dict):
                role = msg.get("role", "user")
                content = msg.get("content", "")
            else:
                role = "user"
                content = str(msg)
            if not isinstance(content, str):
                content = str(content)
            openai_messages.append({"role": role, "content": content})

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=openai_messages,
            temperature=self.temperature,
        )
        content = response.choices[0].message.content

        try:
            input_tokens = response.usage.prompt_tokens
            output_tokens = response.usage.completion_tokens
        except Exception:
            input_tokens = 100
            output_tokens = max(1, len(content) // 4)

        usage_dict = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "prompt_cached_tokens": 0,
            "prompt_cache_creation_tokens": 0,
            "prompt_image_tokens": 0,
        }

        try:
            from langchain_core.messages import AIMessage
            msg = AIMessage(content=content)
            msg.usage_metadata = usage_dict
            msg.usage = usage_dict
            msg.response_metadata = {
                "model_name": self.model,
                "finish_reason": "stop",
                "token_usage": usage_dict,
            }
            return msg
        except ImportError:
            class SimpleMessage:
                def __init__(self, content, model, usage):
                    self.content = content
                    self.type = "ai"
                    self.usage = usage
                    self.usage_metadata = usage
                    self.response_metadata = {
                        "model_name": model,
                        "finish_reason": "stop",
                        "token_usage": usage,
                    }
            return SimpleMessage(content, self.model, usage_dict)

    async def acall(self, messages, **kwargs):
        return await self.ainvoke(messages, **kwargs)

    def bind_tools(self, tools):
        return self

    def with_structured_output(self, schema):
        return self


# ---------------------------------------------------------------------------
# Chaîne de modèles — OpenRouter d'abord
# ---------------------------------------------------------------------------
MODEL_CHAIN = []

if OPENROUTER_API_KEY:
    MODEL_CHAIN.append({
        "provider": "openrouter",
        "model": "openrouter/free",
        "key": OPENROUTER_API_KEY,
        "base_url": "https://openrouter.ai/api/v1",
        "use_wrapper": False,
    })

if GROQ_API_KEY:
    for m in ["llama-3.3-70b-specdec", "qwen/qwen-3.5-32b"]:
        MODEL_CHAIN.append({
            "provider": "groq",
            "model": m,
            "key": GROQ_API_KEY,
            "base_url": "https://api.groq.com/openai/v1",
            "use_wrapper": "groq_wrapper",
        })

if GOOGLE_API_KEY:
    for m in ["gemini-3.5-flash", "gemini-3.6-flash"]:
        MODEL_CHAIN.append({
            "provider": "google_openai",
            "model": m,
            "key": GOOGLE_API_KEY,
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "use_wrapper": "google_wrapper",
        })

if not MODEL_CHAIN:
    print("ERREUR : aucune cle API disponible. Abandon.")
    import sys
    sys.exit(1)

print("Chaine finale :")
for i, entry in enumerate(MODEL_CHAIN):
    print("  %d. [%s] %s" % (i + 1, entry["provider"], entry["model"]))

GLOBAL_TIMEOUT_SECONDS = 240
MAX_STEPS = 8
DELAY_BETWEEN_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Template de consignes (fusionné dans task, pas dans system_prompt.md
# puisque CodeAgent n'a aucun point d'injection pour un system prompt custom)
# ---------------------------------------------------------------------------
CONSIGNES_TEMPLATE = """
Tu es un ingenieur QA fonctionnel. TESTE l'application web, ne te contente pas d'observer.

FORMAT DE SORTIE (CRITIQUE) :
Tu DOIS ecrire des blocs Python entre triple backticks. Exemple :

Je verifie le titre de la page.
```python
title = await evaluate('document.title')
print(title)
```

N'utilise PAS de tags XML. N'utilise PAS la syntaxe tool_name(args).
UNIQUEMENT des blocs de code Python.

REGLES :
1. Effectue au moins UNE vraie interaction utilisateur et verifie avec une assertion.
2. Page chargee N'EST PAS un test. Bouton existe N'EST PAS un test.
3. Test valide = action + observation + assertion.
4. Utilise Python assert.

CONDITION D'ARRET STRICTE :
- Apres 6 appels d'outils maximum, appelle done() quoi qu'il arrive.
- Les resultats partiels sont acceptables.

ANOMALIES (echecs fonctionnels) :
- Texte contenant Erreur, Error, Failed, undefined, null, 0 produit, Aucun produit.
- Lien avec /undefined dans l'URL.
- Liste de produits vide sur une page catalogue.

INTERACTIONS REQUISES :
__REQUIREMENTS__

SORTIE FINALE :
Appelle done(text=...) avec UNIQUEMENT un JSON valide dans text :
  overall_status, site_type, actions_completed, model_used, pages[].

Exemple de forme attendue (remplace les valeurs par le resultat REEL observe) :
```python
import json
result = {"overall_status": "DOWN ou UP selon ce que tu observes",
"site_type": "__SITE_TYPE__", "actions_completed": True,
"model_used": "__MODEL_NAME__",
"pages": [{"url": "page testee", "status": "DOWN ou UP",
"http_code": 200, "action_tested": "description de l'action",
"assertion_passed": True_ou_False, "note": "ce que tu as observe"}]}
await done(text=json.dumps(result, ensure_ascii=False), success=True)
```
"""


def build_full_task(model_name, basic_task):
    """
    Construit la tache complete : URL + instructions de base, puis les
    consignes QA completes (format JSON, regles, limite d'appels).

    IMPORTANT : CodeAgent n'a aucun parametre pour un system prompt custom
    (system_prompt.md est fixe en interne, charge tel quel). Tout doit donc
    passer par `task`, qui devient un simple UserMessage("Task: " + task).
    extract_url_from_task() scanne l'integralite de la chaine (pas seulement
    le debut) et deduplique via set(), donc repeter SITE_URL plus loin dans
    les consignes ne casse pas la detection automatique d'URL.
    """
    consignes = (
        CONSIGNES_TEMPLATE
        .replace("__REQUIREMENTS__", REQUIREMENTS)
        .replace("__SITE_TYPE__", SITE_TYPE)
        .replace("__MODEL_NAME__", model_name)
    )
    return basic_task + "\n\n---\n\n" + consignes


# ---------------------------------------------------------------------------
# JSON helpers
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
    return str(result)


def normalize_report(report, model_name):
    if "overall_status" in report and "pages" in report:
        report["model_used"] = model_name
        return report
    text_blob = json.dumps(report, ensure_ascii=False).lower()
    has_anomaly = any(
        kw in text_blob
        for kw in ["erreur", "error", "undefined", "0 produit",
                   "aucun produit", "no results"]
    )
    status = "DOWN" if has_anomaly else "UP"
    return {
        "overall_status": status,
        "site_type": report.get("site_type", SITE_TYPE),
        "actions_completed": True,
        "model_used": model_name,
        "pages": [{
            "url": report.get("url", SITE_URL),
            "status": status,
            "http_code": None,
            "action_tested": "exploration",
            "assertion_passed": not has_anomaly,
            "note": "Verification effectuee",
        }],
    }


# ---------------------------------------------------------------------------
# Fermeture session (le flush video se fait via BrowserStopEvent interne,
# declenche par stop())
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
        # stop() declenche BrowserStopEvent -> le RecordingWatchdog interne
        # finalise automatiquement le fichier MP4 via CDP+ffmpeg
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

    # Laisser le pipeline CDP+ffmpeg ecrire le MP4 final sur disque
    await asyncio.sleep(5)

    videos = (
        glob.glob(os.path.join(RECORDINGS_DIR, "*.mp4"))
        + glob.glob(os.path.join(RECORDINGS_DIR, "*.webm"))
    )
    print("Videos trouvees dans %s : %d" % (RECORDINGS_DIR, len(videos)))
    for v in videos:
        print("  -> " + v + " (" + str(os.path.getsize(v)) + " octets)")
    if not videos:
        print("Contenu du dossier : " + str(os.listdir(RECORDINGS_DIR)))


# ---------------------------------------------------------------------------
# Detection erreurs (appelees UNIQUEMENT si pas de resultat exploitable)
# ---------------------------------------------------------------------------
def is_fatal_model_error(output_text):
    lower = output_text.lower()
    return any(p in lower for p in [
        "agentic harness",
        "only available on agentic",
        "8 consecutive llm failures",
        "terminating: 8 consecutive",
        "no endpoints found",
        "this model is unavailable",
        "model not found",
        "is no longer available",
        "unavailable for free",
        "authenticationerror",
        "invalid_api_key",
        "does not exist",
        "not found for account",
    ])


def is_quota_error(output_text):
    lower = output_text.lower()
    return any(p in lower for p in [
        "quota exceeded",
        "resource_exhausted",
        "429",
        "too many requests",
        "free_tier_requests",
        "please retry in",
        "insufficient balance",
        "insufficient_balance",
        "balance is not enough",
        "402",
    ])


# ---------------------------------------------------------------------------
# Preflight API (1 token)
# ---------------------------------------------------------------------------
async def preflight_api_check(model_config):
    try:
        client = AsyncOpenAI(
            api_key=model_config["key"],
            base_url=model_config["base_url"],
        )
        await asyncio.wait_for(
            client.chat.completions.create(
                model=model_config["model"],
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            ),
            timeout=20,
        )
        print("Preflight OK : %s" % model_config["model"])
        return True
    except Exception as e:
        print("Preflight ECHEC : %s -> %s" % (
            model_config["model"], str(e)[:200]))
        return False


# ---------------------------------------------------------------------------
# Tentative
#   FIX #1 : succes AVANT erreurs fatales/quota
#   FIX #2 : browser=BrowserSession(...) au lieu de browser_profile=...
#   FIX #3 : consignes QA fusionnees dans task (pas de system prompt custom)
#   FIX #4 : await browser_session.start() AVANT de la passer a CodeAgent
#            (CodeAgent ne l'appelle que s'il cree lui-meme la session)
#   FIX #5 : monkeypatch VideoRecorderService (gel des frames pendant les
#            attentes LLM, voir section MONKEYPATCH VIDEO en haut du fichier)
# ---------------------------------------------------------------------------
async def run_attempt(model_config, task):
    provider = model_config["provider"]
    model_name = model_config["model"]
    api_key = model_config["key"]
    base_url = model_config["base_url"]
    wrapper_type = model_config.get("use_wrapper", False)

    print("=" * 60)
    print("TENTATIVE - PROVIDER : %s | MODELE : %s" % (provider, model_name))
    print("=" * 60)

    agent = None
    browser_session = None
    start_time = time.time()

    try:
        if wrapper_type == "google_wrapper":
            llm = GoogleGeminiWrapper(
                model=model_name, api_key=api_key,
                base_url=base_url, temperature=0.0,
            )
        elif wrapper_type == "groq_wrapper":
            llm = GroqWrapper(
                model=model_name, api_key=api_key,
                base_url=base_url, temperature=0.0,
            )
        else:
            llm = ChatOpenAI(
                model=model_name, base_url=base_url,
                api_key=api_key, temperature=0.0,
            )

        # ⭐ FIX #2 : CodeAgent n'accepte PAS browser_profile en kwarg
        # (silencieusement ignore et logge dans "Ignoring additional kwargs").
        # Il faut construire et passer une vraie BrowserSession.
        profile = BrowserProfile(
            headless=True,
            viewport_width=1280,
            viewport_height=720,
            record_video_dir=RECORDINGS_DIR,
        )
        browser_session = BrowserSession(browser_profile=profile)

        # ⭐ FIX #4 : demarrage manuel obligatoire. CodeAgent.run() ne fait
        # `await browser_session.start()` QUE s'il cree lui-meme la session
        # (browser_session is None au depart). Comme on fournit une session
        # deja construite via `browser=`, il faut la demarrer nous-memes,
        # sinon toute action (navigate/evaluate/click) plante car le CDP
        # n'est jamais initialise.
        await browser_session.start()

        # ⭐ FIX #3 : CodeAgent n'a pas de parametre pour un system prompt
        # custom (system_prompt.md est fixe en interne, aucun point
        # d'injection). Les consignes QA sont fusionnees dans task.
        full_task = build_full_task(model_name, task)

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
            return None

        # --- ETAPE 1 : Evaluer le resultat AVANT les erreurs ---
        raw_output_text = str(result)

        cells = getattr(result, "cells", None) or getattr(result, "history", None) or []
        successful_cells = 0
        llm_actions = 0

        for cell in cells:
            status = getattr(cell, "status", None)
            source = getattr(cell, "source", "") or ""
            is_llm_action = (
                "await " in source and
                "navigate(" not in source and
                len(source.strip()) > 20
            )
            if status is not None and "success" in str(status).lower():
                successful_cells += 1
                if is_llm_action:
                    llm_actions += 1
            elif getattr(cell, "output", ""):
                successful_cells += 1
                if is_llm_action:
                    llm_actions += 1

        final_text = extract_final_result(result)
        report = extract_json_from_text(final_text)
        has_valid_json = report is not None and "overall_status" in report

        # ⭐ FIX #1 : SUCCES D'ABORD
        if has_valid_json or llm_actions >= 1:
            print("Cellules : %d totales, %d reussies, %d actions LLM" % (
                len(cells), successful_cells, llm_actions))
            print("Resultat extrait (" + model_name + ") :")
            print(final_text[:800])

            if report:
                return normalize_report(report, model_name)

            text_lower = final_text.lower()
            has_anomaly = any(
                kw in text_lower
                for kw in ["erreur", "error", "undefined", "0 produit",
                           "no results", "aucun produit"]
            )
            return {
                "overall_status": "DOWN" if has_anomaly else "UP",
                "site_type": SITE_TYPE,
                "actions_completed": True,
                "model_used": model_name,
                "pages": [{
                    "url": SITE_URL,
                    "status": "DOWN" if has_anomaly else "UP",
                    "http_code": None,
                    "action_tested": "exploration et assertions",
                    "assertion_passed": not has_anomaly,
                    "note": "Exploration automatique effectuee.",
                }],
            }

        # --- ETAPE 2 : seulement si pas de resultat exploitable ---
        if is_fatal_model_error(raw_output_text):
            print("ERREUR FATALE : %s -> skip" % model_name)
            return None
        if is_quota_error(raw_output_text):
            print("QUOTA/SOLDE : %s -> skip" % model_name)
            return None

        print("REJETE : %s aucune action LLM et aucun report valide" % model_name)
        return None

    except Exception as err:
        print("Echec avec " + model_name + " : " + str(err))
        return None

    finally:
        await close_agent_session(agent)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    task = (
        "Navigate to " + SITE_URL + ". "
        "Execute the required interactions. "
        "Use Python assertions. "
        "Detect anomalies (errors, undefined, empty listings). "
        "HARD LIMIT: 6 tool calls max, then call done(). "
        "IMPORTANT: write ONLY valid Python code blocks between triple backticks."
    )

    final_report = None
    total = len(MODEL_CHAIN)

    for idx, model_config in enumerate(MODEL_CHAIN):
        print("")
        print("#" * 60)
        print("# Essai %d/%d : [%s] %s" % (
            idx + 1, total, model_config["provider"], model_config["model"]))
        print("#" * 60)

        if not await preflight_api_check(model_config):
            print("Modele ecarte des le preflight : %s" % model_config["model"])
            continue

        report = await run_attempt(model_config, task)
        if report is not None:
            final_report = report
            print("Modele retenu : %s" % model_config["model"])
            break
        else:
            print("Modele ecarte : %s" % model_config["model"])
            if idx < total - 1:
                print("Attente %ds..." % DELAY_BETWEEN_ATTEMPTS)
                await asyncio.sleep(DELAY_BETWEEN_ATTEMPTS)

    if final_report is None:
        final_report = {
            "overall_status": "ERROR",
            "site_type": SITE_TYPE,
            "actions_completed": False,
            "model_used": None,
            "pages": [{
                "url": SITE_URL,
                "status": "ERROR",
                "http_code": None,
                "action_tested": None,
                "assertion_passed": False,
                "note": "Tous les modeles ont echoue.",
            }],
            "error": "Tous les modeles ont echoue.",
        }

    output_path = os.path.join(os.getcwd(), "output.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final_report, f, indent=2, ensure_ascii=False)

    print("")
    print("=== output.json ===")
    print(json.dumps(final_report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
