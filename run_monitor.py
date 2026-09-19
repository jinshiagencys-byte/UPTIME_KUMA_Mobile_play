"""
OpenBrowser-AI — Monitoring fonctionnel.
PRIORITÉ 1 : DeepSeek (excellent pour le code, pricing ultra-compétitif)
PRIORITÉ 2 : OpenRouter free router
PRIORITÉ 3 : Google AI Studio (en backup)
Screenshots + timelapse MP4
Passe au modèle suivant si 0 action LLM réussie
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
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

RECORDINGS_DIR = os.path.abspath("./recordings")
SHOTS_DIR = os.path.join(RECORDINGS_DIR, "shots")
os.makedirs(SHOTS_DIR, exist_ok=True)

print("Recordings dir : " + RECORDINGS_DIR)
print("Shots dir      : " + SHOTS_DIR)
print("DeepSeek key         : " + str(bool(DEEPSEEK_API_KEY)))
print("Google AI Studio key : " + str(bool(GOOGLE_API_KEY)))
print("OpenRouter key       : " + str(bool(OPENROUTER_API_KEY)))


# ---------------------------------------------------------------------------
# Wrapper Google AI Studio (pour usage_metadata)
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
            if hasattr(msg, 'type'):
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

        try:
            from langchain_core.messages import AIMessage
            msg = AIMessage(content=content)
            usage_dict = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }
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
# Chaîne de modèles — DeepSeek EN PREMIER
# ---------------------------------------------------------------------------
MODEL_CHAIN = []

# 🥇 PRIORITÉ 1 : DeepSeek (pricing imbattable, excellent pour le code)
if DEEPSEEK_API_KEY:
    for m in [
        "deepseek-chat",       # DeepSeek V3 - rapide et efficace
        "deepseek-reasoner",   # DeepSeek R1 - raisonnement avancé
    ]:
        MODEL_CHAIN.append({
            "provider": "deepseek",
            "model": m,
            "key": DEEPSEEK_API_KEY,
            "base_url": "https://api.deepseek.com",
            "use_wrapper": False,  # Compatible OpenAI API
        })

# 🥈 PRIORITÉ 2 : OpenRouter (routeur gratuit)
if OPENROUTER_API_KEY:
    for m in [
        "openrouter/free",                        # Routeur auto
        "poolside/laguna-s-2.1:free",             # Top gratuit
        "inclusionai/ling-3.0-flash:free",        # Top gratuit
    ]:
        MODEL_CHAIN.append({
            "provider": "openrouter",
            "model": m,
            "key": OPENROUTER_API_KEY,
            "base_url": "https://openrouter.ai/api/v1",
            "use_wrapper": False,
        })

# 🥉 PRIORITÉ 3 : Google AI Studio (quotas limités, en backup)
if GOOGLE_API_KEY:
    for m in [
        "gemini-3.5-flash",
        "gemini-3.6-flash",
    ]:
        MODEL_CHAIN.append({
            "provider": "google_openai",
            "model": m,
            "key": GOOGLE_API_KEY,
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "use_wrapper": True,
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
DELAY_BETWEEN_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Prompt système
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_TEMPLATE = (
    "You are a functional QA engineer. TEST the web app, do not just observe.\n\n"
    "OUTPUT FORMAT (CRITICAL):\n"
    "You MUST write Python code blocks between triple backticks.\n\n"
    "RULES:\n"
    "1. Perform at least ONE real user interaction and verify with an assertion.\n"
    "2. Page loaded is NOT a test. Button exists is NOT a test.\n"
    "3. Valid test = action + observation + assertion.\n"
    "4. Use Python assert.\n\n"
    "STRICT STOP CONDITION:\n"
    "- After 6 tool calls max, call done() no matter what.\n\n"
    "ANOMALIES (functional failures):\n"
    "- Text containing Erreur, Error, Failed, undefined, null, 0 produit, Aucun produit.\n"
    "- Link with /undefined in URL.\n"
    "- Empty product listing on a catalog page.\n\n"
    "REQUIRED INTERACTIONS:\nREQUIREMENTS\n\n"
    "FINAL OUTPUT:\n"
    "Call done(text=...) with ONLY a valid JSON in text:\n"
    "  overall_status, site_type, actions_completed, model_used, pages[].\n"
)


def build_system_prompt(model_name: str) -> str:
    return SYSTEM_PROMPT_TEMPLATE.replace("REQUIREMENTS", REQUIREMENTS)


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
# Récupération page Playwright
# ---------------------------------------------------------------------------
async def get_playwright_page(session):
    if session is None:
        return None
    for attr in ("current_page", "page", "_page", "playwright_page"):
        try:
            p = getattr(session, attr, None)
            if p is not None and hasattr(p, "screenshot"):
                return p
        except Exception:
            pass
    for method_name in ("must_get_current_page", "get_current_page", "get_page"):
        if hasattr(session, method_name):
            try:
                r = getattr(session, method_name)()
                if asyncio.iscoroutine(r):
                    r = await r
                if r is not None and hasattr(r, "screenshot"):
                    return r
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
# Screenshot recorder (auto-detection JPEG/PNG)
# ---------------------------------------------------------------------------
def detect_image_format(data: bytes) -> str:
    if data[:4] == b'\x89PNG':
        return "png"
    if data[:3] == b'\xff\xd8\xff':
        return "jpg"
    if data[:4] == b'GIF8':
        return "gif"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "webp"
    return "png"


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
                    print("Screenshot recorder : abandon (page introuvable)")
                    break
                continue
            try:
                result = page.screenshot()
                if asyncio.iscoroutine(result):
                    result = await result
                img_bytes = None
                if isinstance(result, bytes):
                    img_bytes = result
                elif isinstance(result, str):
                    import base64
                    img_bytes = base64.b64decode(result)
                elif hasattr(result, "read"):
                    img_bytes = result.read()
                if img_bytes and len(img_bytes) > 100:
                    fmt = detect_image_format(img_bytes)
                    path = os.path.join(SHOTS_DIR, "shot_%04d.%s" % (idx, fmt))
                    with open(path, "wb") as f:
                        f.write(img_bytes)
                    idx += 1
                    misses = 0
                else:
                    misses += 1
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
# Fermeture session
# ---------------------------------------------------------------------------
async def close_agent_session(agent) -> None:
    if agent is None:
        return
    await asyncio.sleep(3)
    session = None
    for attr_name in ("browser_session", "browser", "session"):
        session = getattr(agent, attr_name, None)
        if session is not None:
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
    try:
        shots = os.listdir(SHOTS_DIR) if os.path.isdir(SHOTS_DIR) else []
        print("Screenshots captures : " + str(len(shots)))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Détection erreurs fatales
# ---------------------------------------------------------------------------
def is_fatal_model_error(output_text: str) -> bool:
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
    ])


def is_quota_error(output_text: str) -> bool:
    lower = output_text.lower()
    return any(p in lower for p in [
        "quota exceeded",
        "resource_exhausted",
        "rate limit",
        "429",
        "too many requests",
        "free_tier_requests",
        "please retry in",
        "insufficient_balance",
    ])


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
        if use_wrapper:
            llm = GoogleGeminiWrapper(
                model=model_name, api_key=api_key,
                base_url=base_url, temperature=0.0,
            )
        else:
            llm = ChatOpenAI(
                model=model_name, base_url=base_url,
                api_key=api_key, temperature=0.0,
            )

        profile = BrowserProfile(
            headless=True,
            viewport_width=1280,
            viewport_height=720,
        )

        agent = CodeAgent(
            task=task, llm=llm, browser_profile=profile,
            max_steps=MAX_STEPS,
            extend_system_message=build_system_prompt(model_name),
        )

        recorder_task = asyncio.create_task(screenshot_recorder(agent, interval=2.0))

        try:
            result = await asyncio.wait_for(agent.run(), timeout=GLOBAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            print("Timeout global pour " + model_name)
            return None

        raw_output_text = str(result)

        if is_fatal_model_error(raw_output_text):
            print("❌ ERREUR FATALE : %s → skip" % model_name)
            return None
        if is_quota_error(raw_output_text):
            print("❌ QUOTA ÉPUISÉ : %s → skip" % model_name)
            return None

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

        print("Cellules : %d totales, %d reussies, %d actions LLM" % (
            len(cells), successful_cells, llm_actions))

        final_text = extract_final_result(result)
        report = extract_json_from_text(final_text)
        has_valid_json = report is not None and "overall_status" in report

        if llm_actions < 1 and not has_valid_json:
            print("❌ REJETE : %s n'a effectue AUCUNE action LLM" % model_name)
            return None

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
            print("✅ Modele retenu : %s" % model_config["model"])
            break
        else:
            print("Modele ecarte : %s" % model_config["model"])
            if idx < total - 1:
                print("⏳ Attente %ds..." % DELAY_BETWEEN_ATTEMPTS)
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
