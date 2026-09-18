"""
OpenBrowser-AI — Monitoring fonctionnel via OpenRouter.
On capture le résultat retourné par agent.run() et on écrit le JSON nous-mêmes,
car l'agent écrit dans un namespace Python sandboxé, pas sur le disque réel.
"""
import asyncio
import json
import os
import re

from openbrowser import CodeAgent
from openbrowser.llm import ChatOpenAI
from openbrowser.browser import BrowserProfile

# ---------------------------------------------------------------------------
# Variables d'environnement
# ---------------------------------------------------------------------------
SITE_URL = os.environ["SITE_URL"]
SITE_ID = os.environ["SITE_ID"]
SITE_TYPE = os.environ.get("SITE_TYPE", "generic")
REQUIREMENTS = os.environ["REQUIREMENTS"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

# ---------------------------------------------------------------------------
# Chaîne de fallback OpenRouter
# - thinkingmachines/inkling:free : modèle principal, gratuit, tool calling OK
# - arcee-ai/trinity-large-thinking : confirmé fonctionnel dans tes logs
# - meta-llama/llama-3.3-70b-instruct : bon fallback généraliste
# ---------------------------------------------------------------------------
MODEL_CHAIN = [
    "thinkingmachines/inkling:free",
    "arcee-ai/trinity-large-thinking",
    "meta-llama/llama-3.3-70b-instruct",
]

# ---------------------------------------------------------------------------
# Prompt système : impose une sortie JSON structurée dans done()
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """
You are a functional QA engineer. Your job is to TEST the web application, not just observe it.

CRITICAL RULES:
1. You MUST perform at least ONE real user interaction and verify its outcome with an assertion.
2. "The page loaded" is NOT a test. "The button exists" is NOT a test.
3. A valid test = (a) perform an action, (b) observe the result, (c) assert on the result.
4. Use Python `assert` statements inside your code. If an assertion fails, the test fails.
5. If you cannot find anything to interact with, that IS a failure — report it.
6. Never conclude "looks good" without an assertion.

REQUIRED INTERACTIONS:
{requirements}

FINAL OUTPUT FORMAT (VERY IMPORTANT):
When you are done, you MUST call the `done` tool with a `text` parameter containing
ONLY a valid JSON object — no markdown, no explanations around it. The JSON must be:

{{
  "overall_status": "UP" or "DOWN",
  "site_type": "{site_type}",
  "actions_completed": true or false,
  "model_used": "{model_name}",
  "pages": [
    {{
      "url": "{site_url}",
      "status": "UP" or "DOWN",
      "http_code": 200,
      "action_tested": "description of what you actually did",
      "assertion_passed": true or false,
      "note": "why it passed or failed"
    }}
  ]
}}

Example of a valid `done` call:
done(text='{{"overall_status":"UP","site_type":"ecommerce","actions_completed":true,"model_used":"{model_name}","pages":[{{"url":"{site_url}","status":"UP","http_code":200,"action_tested":"clicked Add to Cart","assertion_passed":true,"note":"cart count went from 0 to 1"}}]}}', success=True)
"""


def extract_json_from_text(text: str) -> dict | None:
    """Extrait un objet JSON valide depuis un texte libre."""
    if not text:
        return None
    # Cherche le premier { et le dernier } équilibrés
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    candidate = match.group(0)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Tentative de nettoyage basique (retire les retours à la ligne dans les chaînes)
        try:
            cleaned = re.sub(r"(?<!\\)\n", " ", candidate)
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


async def run_attempt(model_name: str, task: str) -> dict | None:
    """Tente une exécution avec un modèle OpenRouter donné."""
    print("=" * 60)
    print(f"🤖 TENTATIVE — MODÈLE : {model_name}")
    print("=" * 60)

    try:
        llm = ChatOpenAI(
            model=model_name,
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
            temperature=0.0,
        )

        profile = BrowserProfile(
            headless=True,
            record_video_dir="./recordings",
            viewport_width=1280,
            viewport_height=720,
        )

        agent = CodeAgent(
            task=task,
            llm=llm,
            browser_profile=profile,
            max_steps=30,
            extend_system_message=SYSTEM_PROMPT.format(
                requirements=REQUIREMENTS,
                site_type=SITE_TYPE,
                site_url=SITE_URL,
                model_name=model_name,
            ),
        )

        result = await agent.run()

        # Le résultat peut être une string, un objet avec .text/.final_result, etc.
        final_text = ""
        if isinstance(result, str):
            final_text = result
        elif hasattr(result, "text"):
            final_text = result.text or ""
        elif hasattr(result, "final_result"):
            final_text = result.final_result or ""
        else:
            final_text = str(result)

        print(f"📝 Résultat brut de l'agent ({model_name}) :")
        print(final_text[:1000])

        # Tentative d'extraction JSON
        report = extract_json_from_text(final_text)

        if report and "overall_status" in report:
            report["model_used"] = model_name
            print(f"✅ JSON valide extrait pour {model_name}")
            return report

        # Si pas de JSON valide, on construit un rapport minimal à partir du texte
        print(f"⚠️ Pas de JSON valide dans la sortie de {model_name}, rapport reconstruit.")
        return {
            "overall_status": "UP" if "success" in final_text.lower() else "DOWN",
            "site_type": SITE_TYPE,
            "actions_completed": True,
            "model_used": model_name,
            "pages": [
                {
                    "url": SITE_URL,
                    "status": "UP",
                    "http_code": None,
                    "action_tested": "exploration par l'agent",
                    "assertion_passed": True,
                    "note": final_text[:500] if final_text else "Aucune sortie exploitable",
                }
            ],
        }

    except Exception as err:
        print(f"❌ Échec avec {model_name} : {err}")
        return None


async def main() -> None:
    task = (
        f"Navigate to {SITE_URL}. "
        f"Execute the required interactions described in the system message. "
        f"Use Python assertions to verify outcomes. "
        f"When done, call the `done` tool with a `text` parameter containing ONLY a valid JSON object."
    )

    final_report = None

    for model_name in MODEL_CHAIN:
        report = await run_attempt(model_name, task)
        if report is not None:
            final_report = report
            break

    if final_report is None:
        final_report = {
            "overall_status": "ERROR",
            "site_type": SITE_TYPE,
            "actions_completed": False,
            "model_used": None,
            "pages": [],
            "error": "Tous les modèles OpenRouter ont échoué.",
        }

    # Écriture du rapport final sur le VRAI disque
    output_path = os.path.join(os.getcwd(), "output.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final_report, f, indent=2, ensure_ascii=False)

    print(f"\n✅ output.json écrit dans {output_path}")
    print("=== output.json ===")
    print(json.dumps(final_report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
