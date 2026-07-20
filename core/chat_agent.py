"""
core/chat_agent.py — Groq-powered chatbot agent for extracting custom valuation parameters.
Extracts entities: name, revenue_cr, ebitda_cr, net_worth_cr, is_exporter, customer_type, operating_model, value_chain, industry.
Maps the industry to one of the 320 valid sectors in realdata.db.
Uses pure stdlib urllib.request to hit Groq API (no external SDK required).
"""

import os
import json
import sqlite3
import urllib.request
from typing import Dict, Any, List, Tuple

# We fallback to standard Groq model
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

def get_unique_industries(db_path: str) -> List[str]:
    """Get the unique industry strings from the SQLite database."""
    if not os.path.exists(db_path):
        return []
    try:
        con = sqlite3.connect(db_path)
        rows = con.execute("SELECT DISTINCT industry FROM companies WHERE industry IS NOT NULL AND industry != ''").fetchall()
        con.close()
        return [r[0] for r in rows]
    except Exception:
        return []

def call_groq_api(api_key: str, messages: List[Dict[str, str]], json_mode: bool = False) -> Dict[str, Any]:
    """Call Groq API using stdlib urllib, with automatic SSL fallback for macOS verification bugs."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.2
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
        
    req = urllib.request.Request(
        GROQ_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST"
    )
    
    import ssl
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            res_data = response.read().decode("utf-8")
            return json.loads(res_data)
    except Exception as e:
        # Fallback to unverified SSL context if standard certificate verification fails (macOS common bug)
        if "CERTIFICATE_VERIFY_FAILED" in str(e):
            try:
                context = ssl._create_unverified_context()
                with urllib.request.urlopen(req, timeout=15, context=context) as response:
                    res_data = response.read().decode("utf-8")
                    return json.loads(res_data)
            except Exception as inner_e:
                raise inner_e
        raise e

def run_chat_agent(messages: List[Dict[str, str]], current_state: Dict[str, Any], db_path: str) -> Tuple[str, Dict[str, Any], bool]:
    """
    Processes chat message history and returns:
    (assistant_reply_text, updated_parameters_dict, valuation_ready_boolean)
    """
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        return (
            "⚠️ **Groq API Key missing!** Please start your server with the `GROQ_API_KEY` environment variable set, for example:\n"
            "```bash\nGROQ_API_KEY=gsk_your_key_here python3 server.py\n```",
            current_state,
            False
        )

    # 1. Fetch available industries from database for matching context
    all_industries = get_unique_industries(db_path)
    
    # 2. Construct extraction prompt
    system_prompt = f"""You are a senior financial assistant specializing in Indian MSME comparable valuation.
Your task is to parse a chat conversation and extract the target company parameters into a JSON object.

We have a local SQLite database containing 26,000+ real peers across 320 unique industry sectors.
To value the company, the parameters must align EXACTLY with the database capabilities:

Parameters to extract:
1. "name": Company name (default: "Custom Target Company")
2. "revenue": Annual revenue in INR Crore. Parse expressions like "50 crore", "50 Cr", "500 million" -> convert to Cr (e.g. 50.0).
3. "ebitda": EBITDA in INR Crore.
4. "net_worth": Net Worth / Capital Employed in INR Crore.
5. "industry": Must be matched against the valid list of industry sectors below. If the user mentions a sector, find the closest sector in the list.
6. "operating_model": Must be one of ["manufacturer", "distributor", "retailer", "service", null]. Match based on description (e.g. "we make chemicals" -> manufacturer).
7. "value_chain": Must be one of ["finished_goods", "raw_material", null]. (e.g., supplying raw materials vs final goods).
8. "customer_type": Must be one of ["B2B", "B2C", "mixed", null].
9. "is_exporter": Boolean (true/false) or null.

List of valid Industry sectors in database:
{", ".join(all_industries[:150])} (and others like textiles, paper, power, hotels).
Try to map the user's description to the exact spelling of one of these. If not sure, recommend the top 2-3 most similar sectors and ask the user to choose.

Current parameter state (already extracted):
{json.dumps(current_state, indent=2)}

You MUST output your response in JSON format matching this schema:
{{
  "extracted_params": {{
     "name": string or null,
     "revenue": float or null,
     "ebitda": float or null,
     "net_worth": float or null,
     "industry": string or null,
     "operating_model": string or null,
     "value_chain": string or null,
     "customer_type": string or null,
     "is_exporter": boolean or null
  }},
  "assistant_reply": "Friendly response to the user. Inform them what was extracted. If industry was matched, explain your match. If parameters are missing, ask for them politely. Highlight the remaining fields needed."
}}
"""

    agent_messages = [{"role": "system", "content": system_prompt}] + messages

    try:
        response = call_groq_api(api_key, agent_messages, json_mode=True)
        res_content = response["choices"][0]["message"]["content"]
        res_json = json.loads(res_content)
        
        extracted = res_json.get("extracted_params", {})
        # Merge old state with newly extracted values (favoring non-null values)
        merged = {}
        for k in ["name", "revenue", "ebitda", "net_worth", "industry", "operating_model", "value_chain", "customer_type", "is_exporter"]:
            merged[k] = extracted.get(k) if extracted.get(k) is not None else current_state.get(k)

        # Basic validations
        # Ensure revenue is positive, EBITDA margin is valid if possible
        # Check if valuation is ready: needs at least revenue, ebitda (or net_worth) and industry
        ready = (
            merged.get("revenue") is not None and merged.get("revenue") > 0 and
            (merged.get("ebitda") is not None or merged.get("net_worth") is not None) and
            merged.get("industry") is not None
        )
        
        reply = res_json.get("assistant_reply", "Processed your message successfully.")
        return reply, merged, ready

    except Exception as e:
        return f"Error interacting with LLM: {str(e)}", current_state, False
