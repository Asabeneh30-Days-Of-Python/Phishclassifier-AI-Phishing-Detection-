# api/attacker_insights.py
import re
import logging
from urllib.parse import urlparse
from typing import List, Dict, Any

_logger = logging.getLogger(__name__)

# Lazy-loaded NLP resources
_NLTK_SID = None
_SPACY_NLP = None
_TRIED_SPACY_LOAD = False

def _get_vader():
    """
    Lazy-load and return a VADER SentimentIntensityAnalyzer instance.
    Returns None if unavailable.
    """
    global _NLTK_SID
    if _NLTK_SID is not None:
        return _NLTK_SID
    try:
        from nltk.sentiment.vader import SentimentIntensityAnalyzer  # type: ignore[reportMissingImports]
        _NLTK_SID = SentimentIntensityAnalyzer()
        return _NLTK_SID
    except Exception as e:
        _logger.warning("VADER unavailable: %s", e)
        _NLTK_SID = None
        return None

def _get_spacy_nlp():
    """
    Lazy-load and return a spaCy nlp pipeline.
    Falls back to a blank English pipeline if en_core_web_sm is not present.
    Returns None if spaCy itself is unavailable.
    """
    global _SPACY_NLP, _TRIED_SPACY_LOAD
    if _SPACY_NLP is not None:
        return _SPACY_NLP
    if _TRIED_SPACY_LOAD:
        return _SPACY_NLP
    _TRIED_SPACY_LOAD = True
    try:
        import spacy
        try:
            _SPACY_NLP = spacy.load("en_core_web_sm")
        except Exception:
            # fallback to blank model to still support basic tokenization/NER interfaces
            _SPACY_NLP = spacy.blank("en")
        return _SPACY_NLP
    except Exception as e:
        _logger.warning("spaCy unavailable: %s", e)
        _SPACY_NLP = None
        return None

def analyze_sentiment(email_text: str) -> Dict[str, float]:
    """
    Analyze the overall sentiment of the email using VADER when available.
    Returns a dict with keys 'neg','neu','pos','compound' with safe defaults.
    """
    sid = _get_vader()
    if sid is None:
        return {"neg": 0.0, "neu": 1.0, "pos": 0.0, "compound": 0.0}
    try:
        return sid.polarity_scores(email_text or "")
    except Exception as e:
        _logger.warning("VADER scoring failed: %s", e)
        return {"neg": 0.0, "neu": 1.0, "pos": 0.0, "compound": 0.0}

def extract_entities(email_text: str) -> List[tuple]:
    """
    Extract named entities from the email using spaCy when available.
    Returns list of (text,label) tuples; empty list if unavailable.
    """
    nlp = _get_spacy_nlp()
    if nlp is None:
        return []
    try:
        doc = nlp(email_text or "")
        return [(ent.text, ent.label_) for ent in doc.ents]
    except Exception as e:
        _logger.warning("spaCy NER failed: %s", e)
        return []

def extract_urls(email_text: str) -> List[str]:
    """
    Extract URLs from the email using a robust, permissive regex.
    """
    if not isinstance(email_text, str):
        email_text = str(email_text or "")
    url_regex = r'https?://[^\s)>\'"]+'
    try:
        return re.findall(url_regex, email_text)
    except Exception:
        return []

def _hostname_of_url(u: str) -> str:
    try:
        parsed = urlparse(u)
        return (parsed.netloc or parsed.path).lower()
    except Exception:
        return u.lower()

def check_url_similarity(url: str, known_domains: List[str], threshold: float = 80.0) -> Dict[str, float]:
    """
    Compare the given URL (host portion) with a list of trusted domains using fuzzy matching.
    If rapidfuzz is unavailable, returns an empty dict and logs a warning.
    """
    try:
        from rapidfuzz import fuzz  # type: ignore[reportMissingImports]
    except Exception:
        _logger.warning("rapidfuzz not available for URL similarity checks")
        return {}

    host = _hostname_of_url(url)
    similarities: Dict[str, float] = {}
    for domain in known_domains:
        try:
            score = fuzz.partial_ratio(host, domain.lower())
            similarities[domain] = float(score)
        except Exception:
            similarities[domain] = 0.0
    return {d: s for d, s in similarities.items() if s >= float(threshold)}

def analyze_attacker_motives(email_text: str) -> Dict[str, Any]:
    """
    Analyze the email content to infer potential attacker motives and provide actionable recommendations.
    Uses rule-based checks, sentiment analysis, NER, and fuzzy URL matching where available.
    """
    motives = set()
    recommendations = set()
    detailed_reasoning = []

    lower_text = (email_text or "").lower()

    # Rule-based cues
    if "verify" in lower_text and "account" in lower_text:
        motives.add("Credential Harvesting")
        recommendations.add("Avoid clicking links and verify the sender via an independent channel.")
        detailed_reasoning.append("The email prompts to 'verify' your account, a common credential-harvesting tactic.")
    if any(tok in lower_text for tok in ("free", "won", "prize")):
        motives.add("Financial Fraud")
        recommendations.add("Offers that seem too good to be true could be scams; do not share personal details.")
        detailed_reasoning.append("Monetary lure language suggests potential financial fraud.")
    if any(tok in lower_text for tok in ("urgent", "immediate")):
        motives.add("Speedy Exploitation")
        recommendations.add("Take extra time to review urgent requests to ensure they are legitimate.")
        detailed_reasoning.append("Urgent language may be used to pressure quick action.")

    # Sentiment analysis
    sentiment = analyze_sentiment(email_text)
    try:
        comp = float(sentiment.get("compound", 0.0))
    except Exception:
        comp = 0.0
    if comp < -0.3:
        motives.add("Manipulation via Negative Sentiment")
        recommendations.add("High negative sentiment may be used to instill panic; verify with trusted sources.")
        detailed_reasoning.append(f"Negative sentiment (compound {comp}) suggests emotional manipulation.")

    # Named entity signals
    entities = extract_entities(email_text)
    if not any(label == "ORG" for _, label in entities):
        motives.add("Missing Organizational Context")
        recommendations.add("Verify sender details through known official channels.")
        detailed_reasoning.append("No ORG entities detected; may indicate a non-official sender.")

    # URL analysis
    trusted_domains = ["amazon.com", "google.com", "microsoft.com"]
    for u in extract_urls(email_text):
        matches = check_url_similarity(u, trusted_domains)
        if matches:
            motives.add("Suspicious URL Mimicry")
            recommendations.add(f"URL {u} is similar to known domains: {', '.join(matches.keys())}. Verify independently.")
            detailed_reasoning.append(f"URL {u} resembles trusted domains ({', '.join(matches.keys())}).")

    if not motives:
        motives.add("Unknown or Low Risk")
        recommendations.add("Review the email content carefully and verify with trusted sources if in doubt.")
        detailed_reasoning.append("No significant phishing cues detected.")

    return {
        "motives": list(motives),
        "recommendations": list(recommendations),
        "detailed_reasoning": detailed_reasoning
    }
