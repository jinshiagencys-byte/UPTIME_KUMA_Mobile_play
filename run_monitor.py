"""
OpenBrowser-AI — Monitoring fonctionnel.
Google AI Studio via wrapper custom OpenAI (contourne le bug frequency_penalty)
Fallback OpenRouter (Gemma 3, Qwen-VL, Llama)
Screenshots + timelapse MP4
Passe au modèle suivant si 0 action réussie
"""
import asyncio
import json
import os
import re
from openai import AsyncOpenAI
from openbrowser import CodeAgent
from openbrowser.llm import ChatOpenAI
from openbrowser.browser import BrowserProfile

# ---------------------------------------------------------------------------
# Env
# ---------------------------------------------------------------------------
SITE_URL = os.environ.get("SITE_URL", "")
SITE_ID = os.environ.get("SITE_ID", "")
SITE_TYPE = os.environ.get("SITE_TYPE", "generic")
REQUIREMENTS = os.environ.get("REQUIREMENTS", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")

RECORDINGS_DIR = os.path.abspath("./recordings")
SHOTS_DIR = os.path.join(RECORDINGS_DIR, "shots")
os.makedirs(SHOTS_DIR, exist_ok=True)

print("Recordings dir : " + RECORDINGS_DIR)
print("Shots dir      : " + SHOTS_DIR)
print("Google AI Studio key : " + str(bool(GOOGLE_API_KEY)))
print("OpenRouter key       : " + str(bool(OPENROUTER_API_KEY)))

# ---------------------------------------------------------------------------
# Wrapper custom pour Google AI Studio (contourne le bug frequency_penalty)
# ---------------------------------------------------------------------------
class GoogleGeminiWrapper:
    """
    Wrapper LLM pour Google AI Studio via endpoint OpenAI-compatible.
    N'envoie PAS frequency_penalty ni presence_penalty (non supportés par Google).
    Compatible avec l'interface attendue par CodeAgent (ainvoke).
    """
    
    def __init__(self, model, api_key, base_url, temperature=0.0):
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.temperature = temperature
    
    async def ainvoke(self, messages):
        """Appelle l'API Google AI Studio sans paramètres non supportés"""
        # Convertir les messages (LangChain ou dicts)
        openai_messages = []
        for msg in messages:
            if hasattr(msg, 'type'):
                # LangChain message
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
            openai_messages.append({"role": role, "content": content})
        
        # Appel API SANS frequency_penalty ni presence_penalty
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=openai_messages,
            temperature=self.temperature,
            # IMPORTANT : ne pas envoyer frequency_penalty, presence_penalty, etc.
        )
        
        content = response.choices[0].message.content
        
        # Retourner un objet compatible avec LangChain
        try:
            from langchain_core.messages import AIMessage
            return AIMessage(content=content)
        except ImportError:
            # Fallback si langchain_core n'est pas disponible
            class SimpleMessage:
                def __init__(self, content):
                    self.content = content
            return SimpleMessage(content=content)

# ---------------------------------------------------------------------------
# Chaîne de modèles : Google AI Studio (wrapper custom) + OpenRouter
# ---------------------------------------------------------------------------
MODEL_CHAIN = []

# Google AI Studio via wrapper custom (contourne le bug frequency_penalty)
if GOOGLE_API_KEY:
    for m in [
        "gemini-3.5-flash",     # Le plus rapide
        "gemini-3.6-flash",     # Plus de contexte
        "gemini-2.5-pro",       # Plus puissant
    ]:
        MODEL_CHAIN.append({
            "provider": "google_openai",
            "model": m,
            "key": GOOGLE_API_KEY,
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "use_wrapper": True,  # Utiliser notre wrapper custom
        })

# OpenRouter - modèles gratuits à jour (septembre 2026)
if OPENROUTER_API_KEY:
    for m in [
        "google/gemma-3-27b-it:free",
        "qwen/qwen-2.5-vl-7b-instruct:free",
        "meta-llama/llama-3.3-70b-instruct:free",
    ]:
        MODEL_CHAIN.append({
            "provider": "openrouter",
            "model": m,
            "key": OPENROUTER_API_KEY,
            "base_url": "https://openrouter.ai/api/v1",
            "use_wrapper": False,  # Utiliser ChatOpenAI standard
        })

if not MODEL_CHAIN:
    print("ERREUR : aucune cle API disponible. Abandon.")
    import sys
    sys.exit(1)

print("Chaine finale :")
for i, entry in enumerate(MODEL_CHAIN):
    print("  %d. [%s] %s" % (i + 1, entry["provider"], entry["model"]))

GLOBAL_TIMEOUT_SECONDS = 300
MAX_STEPS = 12

# ---------------------------------------------------------------------------
# Prompt système
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_TEMPLATE = (
    "You are a functional QA engineer. TEST the web app, do not just observe.\n"
    "\n"
    "OUTPUT FORMAT (CRITICAL):\n"
    "You MUST write Python code blocks between triple backticks. Example:\n"
    "\n"
    "I will check the page title.\n"
    "```python\n"
    "title = await evaluate('document.title')\n"
    "print(title)\n"
    "```\n"
    "\n"
    "Do NOT use XML tags. Do NOT use tool_name(args) syntax.\n"
    "ONLY Python code blocks.\n"
    "\n"
    "RULES:\n"
    "1. Perform at least ONE real user interaction and verify with an assertion.\n"
    "2. Page loaded is NOT a test. Button exists is NOT a test.\n"
    "3. Valid test = action + observation + assertion.\n"
    "4. Use Python assert.\n"
    "\n"
    "STRICT STOP CONDITION:\n"
    "- After 6 tool calls max, call done() no matter what.\n"
    "- Partial results are OK.\n"
    "\n"
    "ANOMALIES (functional failures):\n"
    "- Text containing Erreur, Error, Failed, undefined, null, 0 produit, Aucun produit.\n"
    "- Link with /undefined in URL.\n"
    "- Empty product listing on a catalog page.\n"
    "\n"
    "REQUIRED INTERACTIONS:\n"
    "REQUIREMENTS\n"
    "\n"
    "FINAL OUTPUT:\n"
    "Call done(text=...) with ONLY a valid JSON in text:\n"
    "  overall_status, site_type, actions_completed, model_used, pages[].\n"
    "\n"
    "Example done:\n"
    "```python\n"
    "import json\n"
    "result = {\"overall_status\": \"DOWN\", \"site_type\": \"__SITE_TYPE__\", "
    "\"actions_completed\": True, \"model_used\": \"__MODEL_NAME__\", "
    "\"pages\": [{\"url\": \"__SITE_URL__\", \"status\": \"DOWN\", "
    "\"http_code\": 200, \"action_tested\": \"search\", "
    "\"assertion_passed\": False, \"note\": \"0 produit\"}]}\n"
    "await done(text=json.dumps(result, ensure_ascii=False), success=True)\n"
    "```\n"
)

def build_system_prompt(model_name: str) -> str:
    return (
        SYSTEM_PROMPT_TEMPLATE
        .replace("REQUIREMENTS", REQUIREMENTS)
        .replace("__SITE_TYPE__", SITE_TYPE)
        .replace("__MODEL_NAME__", model_name)
        .replace("__SITE_URL__", SITE_URL)
    )

# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------
def extract_json_from_text(text: str):
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

def extract_final_result(result) -> str:
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
        match = re.search(
            r"done\s*\(\s*text\s*=\s*json\.dumps\s*\(\s*([a-zA-Z_][a-zA-Z0-9_]*)",
            source, re.DOTALL,
        )
        if match:
            output = getattr(cell, "output", "") or ""
            if output.strip():
                return output
        return source

    for cell in reversed(list(cells)):
        output = getattr(cell, "output", "") or ""
        if '"url"' in output or '"overall_status"' in output:
            return output
    return str(result)

def normalize_report(report: dict, model_name: str) -> dict:
    if "overall_status" in report and "pages" in report:
        report["model_used"] = model_name
        return report

    text_blob = json.dumps(report, ensure_ascii=False).lower()
    has_anomaly = any(
        kw in text_blob
        for kw in ["erreur", "error", "undefined", "0 produit",
                   "aucun produit", "no results", "anomalies_detected"]
    )
    status = "DOWN" if has_anomaly else "UP"
    pages = [{
        "url": report.get("url", SITE_URL),
        "status": status,
        "http_code": None,
        "action_tested": "exploration",
        "assertion_passed": not has_anomaly,
        "note": report.get("done_summary") or report.get("message") or "Verification effectuee",
    }]
    return {
        "overall_status": status,
        "site_type": report.get("site_type", SITE_TYPE),
        "actions_completed": True,
        "model_used": model_name,
        "pages": pages,
    }

# ---------------------------------------------------------------------------
# Récupération de la page Playwright
# ---------------------------------------------------------------------------
async def get_playwright_page(session):
    if session is None:
        return None
    for method_name in ("must_get_current_page", "get_current_page"):
        if hasattr(session, method_name):
            try:
                r = getattr(session, method_name)()
                if asyncio.iscoroutine(r):
                    r = await r
                if r is not None and hasattr(r, "screenshot"):
                    return r
            except Exception:
                pass
    for attr in ("current_page", "page", "_page"):
        try:
            p = getattr(session, attr, None)
            if p is not None and hasattr(p, "screenshot"):
                return p
        except Exception:
            pass
    if hasattr(session, "get_pages"):
        try:
            r = session.get_pages()
            if asyncio.iscoroutine(r):
                r = await r
            if r and len(r) > 0:
                return r[0]
        except Exception:
            pass
    return None

# ---------------------------------------------------------------------------
# Screenshot recorder (CORRIGÉ : sans full_page)
# ---------------------------------------------------------------------------
async def screenshot_recorder(agent, interval=2.0):
    idx = 0
    misses = 0
    while True:
        try:
            await asyncio.sleep(interval)
            session = getattr(agent, "browser_session", None)
            if session is None:
                misses += 1
                continue
            page = await get_playwright_page(session)
            if page is None:
                misses += 1
                if misses > 30:
                    print("Screenshot recorder : abandon apres %d echecs" % misses)
                    break
                continue
            path = os.path.join(SHOTS_DIR, "shot_%04d.png" % idx)
            try:
                # CORRIGÉ : appeler screenshot() SANS arguments (pas de full_page)
                screenshot_bytes = page.screenshot()
                if asyncio.iscoroutine(screenshot_bytes):
                    screenshot_bytes = await screenshot_bytes
                # Écrire les bytes dans le fichier
                with open(path, "wb") as f:
                    f.write(screenshot_bytes)
                idx += 1
                misses = 0
            except Exception as e:
                if idx == 0:
                    print("Screenshot erreur : " + str(e))
                misses += 1
        except asyncio.CancelledError:
            print("Screenshot recorder arrete (%d shots)" % idx)
            break
        except Exception:
            misses += 1

# ---------------------------------------------------------------------------
# Fermeture de session
# ---------------------------------------------------------------------------
async def close_agent_session(agent) -> None:
    if agent is None:
        return
    session = None
    for attr_name in ("browser_session", "browser", "session"):
        session = getattr(agent, attr_name, None)
        if session is not None:
            print("Session trouvee via agent." + attr_name)
            break
    if session is None:
        return
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
    await asyncio.sleep(3)
    try:
        shots = os.listdir(SHOTS_DIR) if os.path.isdir(SHOTS_DIR) else []
        print("Screenshots captures : " + str(len(shots)))
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Tentative
# ---------------------------------------------------------------------------
async def run_attempt(model_config: dict, task: str):
    provider = model_config["provider"]
    model_name = model_config["model"]
    api_key = model_config["key"]
    base_url = model_config["base_url"]
    use_wrapper = model_config.get("use_wrapper", False)

    print("=" * 60)
    print("TENTATIVE - PROVIDER : %s | MODELE : %s" % (provider, model_name))
    print("=" * 60)

    agent = None
    recorder_task = None

    try:
        # Utiliser le wrapper custom pour Google AI Studio, ChatOpenAI pour OpenRouter
        if use_wrapper:
            llm = GoogleGeminiWrapper(
                model=model_name,
                api_key=api_key,
                base_url=base_url,
                temperature=0.0,
            )
        else:
            llm = ChatOpenAI(
                model=model_name,
                base_url=base_url,
                api_key=api_key,
                temperature=0.0,
            )

        profile = BrowserProfile(
            headless=True,
            viewport_width=1280,
            viewport_height=720,
        )

        agent = CodeAgent(
            task=task,
            llm=llm,
            browser_profile=profile,
            max_steps=MAX_STEPS,
            extend_system_message=build_system_prompt(model_name),
        )

        recorder_task = asyncio.create_task(screenshot_recorder(agent, interval=2.0))

        try:
            result = await asyncio.wait_for(agent.run(), timeout=GLOBAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            print("Timeout global pour " + model_name)
            return None

        # Compter les cellules réussies
        cells = getattr(result, "cells", None) or getattr(result, "history", None) or []
        successful_cells = 0
        for cell in cells:
            status = getattr(cell, "status", None)
            if status is not None and "success" in str(status).lower():
                successful_cells += 1
            elif getattr(cell, "output", ""):
                successful_cells += 1

        print("Cellules totales : %d, reussies : %d" % (len(cells), successful_cells))
        
        # Validation renforcée : minimum 2 actions réussies
        if successful_cells < 2:
            print("ATTENTION : %s n'a effectue seulement %d action(s) reussie(s) - insuffisant" % (model_name, successful_cells))
            return None

        final_text = extract_final_result(result)
        print("Resultat extrait (" + model_name + ") :")
        print(final_text[:800])

        report = extract_json_from_text(final_text)
        if report:
            return normalize_report(report, model_name)

        text_lower = final_text.lower()
        has_anomaly = any(
            kw in text_lower
            for kw in ["erreur", "error", "undefined", "0 produit",
                       "no results", "aucun produit", "anomalies_detected"]
        )

        anomalies = []
        if "erreur lors du chargement des sliders" in text_lower:
            anomalies.append("carrousel casse")
        if "erreur lors du chargement des marques" in text_lower:
            anomalies.append("marques non chargees")
        if "0 produit" in text_lower:
            anomalies.append("catalogue vide")

        clean_note = ("Anomalies detectees : " + ", ".join(anomalies) + ".") if anomalies \
            else "Exploration automatique (aucune anomalie detectee)."

        return {
            "overall_status": "DOWN" if has_anomaly else "UP",
            "site_type": SITE_TYPE,
            "actions_completed": True,
            "model_used": model_name,
            "pages": [{
                "url": SITE_URL,
                "status": "DOWN" if has_anomaly else "UP",
                "http_code": None,
                "action_tested": "exploration et assertions par l'agent",
                "assertion_passed": not has_anomaly,
                "note": clean_note,
            }],
        }

    except Exception as err:
        print("Echec avec " + model_name + " : " + str(err))
        return None

    finally:
        if recorder_task is not None:
            recorder_task.cancel()
            try:
                await recorder_task
            except (asyncio.CancelledError, Exception):
                pass
        await close_agent_session(agent)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
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

        report = await run_attempt(model_config, task)
        if report is not None:
            final_report = report
            print("Modele retenu : %s (%s)" % (
                model_config["model"], model_config["provider"]))
            break
        else:
            print("Modele ecarte : %s, on passe au suivant" % model_config["model"])

    if final_report is None:
        final_report = {
            "overall_status": "ERROR",
            "site_type": SITE_TYPE,
            "actions_completed": False,
            "model_used": None,
            "pages": [],
            "error": "Tous les modeles ont echoue ou n'ont effectue aucune action.",
        }

    output_path = os.path.join(os.getcwd(), "output.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final_report, f, indent=2, ensure_ascii=False)

    print("")
    print("output.json ecrit dans " + output_path)
    print("=== output.json ===")
    print(json.dumps(final_report, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    asyncio.run(main())
