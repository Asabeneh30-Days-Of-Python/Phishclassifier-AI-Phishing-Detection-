import os, json
import openai   # type: ignore[reportMissingImports]
from tqdm import tqdm

# 1) configure your key & path
openai.api_key = os.getenv("OPENAI_API_KEY")
OUT = "data/enterprise_synth.json"
N = 1000
PHISH_RATIO = 0.3

prompt = f"""
Generate {N} JSON objects, each with two fields:
  • "text" containing a realistic enterprise email
  • "label" 0 for a normal internal email, 1 for a phishing attempt
Exactly {int(PHISH_RATIO*100)}% should be phishing attempts that ask to click a link.
Output as a JSON array, e.g.:

[{{"text":"…","label":0}}, …]
"""

resp = openai.ChatCompletion.create(
    model="gpt-4",
    messages=[{"role":"user","content": prompt}],
    temperature=0.7,
    max_tokens=2000
)

# 2) parse & save
data = json.loads(resp.choices[0].message.content)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print(f"Wrote {len(data)} examples to {OUT}")