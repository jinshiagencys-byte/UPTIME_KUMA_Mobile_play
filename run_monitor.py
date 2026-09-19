"""
OpenBrowser-AI — Monitoring fonctionnel.
STRATÉGIE 100% GRATUITE + VIDÉO NATIVE :
  1. Groq (llama-3.3-70b-specdec, qwen-3.5-32b)
  2. Google AI Studio (gemini-3.5-flash / 3.6-flash)
  3. OpenRouter (openrouter/free)
Enregistrement vidéo natif via record_video_dir (plus de screenshots+ffmpeg).
Preflight + détection erreurs fatales + quotas.
"""
import asyncio
import glob
import json
import os
import re
import time

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
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

RECORDINGS_DIR = os.path.abspath("./recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

print("Recordings dir : " + RECORDINGS_DIR)
print("Groq key          : " + str(bool(GROQ_API_KEY)))
print("Google AI Std key : " + str(bool(GOOGLE_API_KEY)))
print("OpenRouter key    : " + str(bool(OPENROUTER_API_KEY)))


# ---------------------------------------------------------------------------
# Wrapper Google AI Studio — TOUS les champs requis par TokenUsageEntry
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
                # Normaliser content en string (sinon Groq/Google rejettent)
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

        # ⚠️ TOUS les 5 champs exigés par TokenUsageEntry de openbrowser-ai
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
# Wrapper Groq — normalise le content en string (corrige l'erreur multimodal)
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

            # ⚠️ Groq exige du string pur — pas de listes multimodales
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
# Chaîne de modèles — 100% GRATUITE
# ---------------------------------------------------------------------------
MODEL_CHAIN = []

# 🥇 GROQ : modèles à jour sept. 2026
if GROQ_API_KEY:
    for m in ["llama-3.3-70b-specdec", "qwen/qwen-3.5-32b"]:
        MODEL_CHAIN.append({
            "provider": "groq",
            "model": m,
            "key": GROQ_API_KEY,
            "base_url": "https://api.groq.com/openai/v1",
            "use_wrapper": "groq_wrapper",
        })

# 🥈 GOOGLE AI STUDIO : en secours (quotas limités)
if GOOGLE_API_KEY:
    for m in ["gemini-3.5-flash", "gemini-3.6-flash"]:
        MODEL_CHAIN.append({
            "provider": "google_openai",
            "model": m,
            "key": GOOGLE_API_KEY,
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "use_wrapper": "google_wrapper",
        })

# 🥉 OPENROUTER : routeur gratuit en dernier recours
if OPENROUTER_API_KEY:
    MODEL_CHAIN.append({
        "provider": "openrouter",
        "model": "openrouter/free",
        "key": OPENROUTER_API_KEY,
        "base_url": "https://openrouter.ai/api/v1",
        "use_wrapper": False,
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
# Prompt système
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_TEMPLATE = (
    "You are a functional QA engineer. TEST the web app, do not just observe.\n\n"
    "OUTPUT FORMAT (CRITICAL):\n"
    "You MUST write Python code blocks between triple backticks. Example:\n\n"
    "I will check the page title.\n"
    "```python\n"
    "title = await evaluate('document.title')\n"
    "print(title)\n"
    "```\n\n"
    "Do NOT use XML tags. Do NOT use tool_name(args) syntax.\n"
    "ONLY Python code blocks.\n\n"
    "RULES:\n"
    "1. Perform at least ONE real user interaction and verify with an assertion.\n"
    "2. Page loaded is NOT a test. Button exists is NOT a test.\n"
    "3. Valid test = action + observation + assertion.\n"
    "4. Use Python assert.\n\n"
    "STRICT STOP CONDITION:\n"
    "- After 6 tool calls max, call done() no matter what.\n"
    "- Partial results are OK.\n\n"
    "ANOMALIES (functional failures):\n"
    "- Text containing Erreur, Error, Failed, undefined, null, 0 produit, Aucun produit.\n"
    "- Link with /undefined in URL.\n"
    "- Empty product listing on a catalog page.\n\n"
    "REQUIRED INTERACTIONS:\nREQUIREMENTS\n\n"
    "FINAL OUTPUT:\n"
    "Call done(text=...) with ONLY a valid JSON in text:\n"
    "  overall_status, site_type, actions_completed, model_used, pages[].\n"
)


def build_system_prompt(model_name):
    return SYSTEM_PROMPT_TEMPLATE.replace("REQUIREMENTS", REQUIREMENTS)


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
# Fermeture session
# ---------------------------------------------------------------------------
async def close_agent_session(agent):
    if agent is None:
        return
    await asyncio.sleep(3)
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
    # Lister les vidéos enregistrées nativement
    videos = glob.glob(os.path.join(RECORDINGS_DIR, "*.webm")) + \
             glob.glob(os.path.join(RECORDINGS_DIR, "*.mp4"))
    print("Videos natives : " + str(len(videos)))
    for v in videos:
        print("  -> " + v + " (" + str(os.path.getsize(v)) + " octets)")


# ---------------------------------------------------------------------------
# Détection erreurs
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
        "rate limit",
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
# Preflight API (1 token) avant lancement navigateur
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
# Tentative — avec enregistrement vidéo NATIF
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

        # ⭐ ENREGISTREMENT VIDÉO NATIF — plus besoin de screenshots !
        profile = BrowserProfile(
            headless=True,
            viewport_width=1280,
            viewport_height=720,
            record_video_dir=RECORDINGS_DIR,  # ← vidéo native .webm
        )

        agent = CodeAgent(
            task=task, llm=llm, browser_profile=profile,
            max_steps=MAX_STEPS,
            extend_system_message=build_system_prompt(model_name),
        )

        try:
            result = await asyncio.wait_for(
                agent.run(), timeout=GLOBAL_TIMEOUT_SECONDS)
            print("agent.run() termine en %.1fs" % (time.time() - start_time))
        except asyncio.TimeoutError:
            print("Timeout global pour " + model_name)
            return None

        raw_output_text = str(result)

        if is_fatal_model_error(raw_output_text):
            print("ERREUR FATALE : %s -> skip" % model_name)
            return None
        if is_quota_error(raw_output_text):
            print("QUOTA/SOLDE : %s -> skip" % model_name)
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
            print("REJETE : %s n'a effectue AUCUNE action LLM" % model_name)
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
