"""
OpenBrowser-AI — Monitoring fonctionnel avec CodeAgent.
Chaîne de fallback Groq + assertions Python + enregistrement vidéo.
"""
import asyncio
import json
import os
import sys

from openbrowser import CodeAgent, ChatGroq
from openbrowser.browser import BrowserProfile

# ---------------------------------------------------------------------------
# Variables d'environnement
# ---------------------------------------------------------------------------
SITE_URL = os.environ["SITE_URL"]
SITE_ID = os.environ["SITE_ID"]
SITE_TYPE = os.environ.get("SITE_TYPE", "generic")
REQUIREMENTS = os.environ["REQUIREMENTS"]

# ---------------------------------------------------------------------------
# Chaîne de fallback Groq — du plus capable au plus léger.
# On évite gpt-oss-120b à cause du bug de tool calling multi-tour.
# ---------------------------------------------------------------------------
MODEL_CHAIN = [
    "openai/gpt-oss-20b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "llama-3.3-70b-versatile",
]

# ---------------------------------------------------------------------------
# Prompt système : force le test fonctionnel avec assertions
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """
You are a functional QA engineer. Your job is to TEST the web application, not just observe it.

CRITICAL RULES:
1. You MUST perform at least ONE real user interaction and verify its outcome with an assertion.
2. "The page loaded" is NOT a test. "The button exists" is NOT a test.
3. A valid test = (a) perform an action, (b) observe the result, (c) assert on the result.
4. Use Python `assert` statements. If an assertion fails, the test fails.
5. If you cannot find anything to interact with, that IS a failure — report it.
6. Never conclude "looks good" without an assertion.

REQUIRED INTERACTIONS:
{requirements}

After completing the test, write a JSON report to output.json with:
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
"""


async def run_attempt(model_name: str, task: str) -> dict | None:
    """Tente une exécution avec un modèle donné. Retourne le rapport ou None."""
    print("=" * 60)
    print(f"🤖 TENTATIVE — MODÈLE : {model_name}")
    print("=" * 60)

    try:
        llm = ChatGroq(
            model=model_name,
            api_key=os.environ["GROQ_API_KEY"],
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

        await agent.run()

        # Lire le rapport généré par l'agent
        if os.path.exists("output.json"):
            with open("output.json", "r", encoding="utf-8") as f:
                report = json.load(f)
            report["model_used"] = model_name
            return report

        print(f"⚠️ {model_name} n'a pas produit de output.json.")
        return None

    except Exception as err:
        print(f"❌ Échec avec {model_name} : {err}")
        return None


async def main() -> None:
    task = (
        f"Navigate to {SITE_URL}. "
        f"Execute the required interactions. "
        f"Use Python assertions to verify outcomes. "
        f"Write the JSON report to output.json when done."
    )

    final_report = None

    for model_name in MODEL_CHAIN:
        report = await run_attempt(model_name, task)
        if report is not None:
            final_report = report
            break

    if final_report is None:
        # Aucun modèle n'a fonctionné → rapport d'erreur
        final_report = {
            "overall_status": "ERROR",
            "site_type": SITE_TYPE,
            "actions_completed": False,
            "model_used": None,
            "pages": [],
            "error": "Tous les modèles Groq ont échoué.",
        }

    # Écrire le rapport final à la racine (pour l'upload artifact)
    with open("../output.json", "w", encoding="utf-8") as f:
        json.dump(final_report, f, indent=2, ensure_ascii=False)

    print("\n=== output.json ===")
    print(json.dumps(final_report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
