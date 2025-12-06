import re
import string
import email
from collections import Counter
from urllib.parse import urlparse

# Try standard import path; ignore in-editor complaints if not yet installed
try:
    from vaderSentiment import SentimentIntensityAnalyzer   # type: ignore[reportMissingImports]
except ImportError:
    # type: ignore[reportMissingImports]
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer   # type: ignore[reportMissingImports]

# instantiate once for performance
SENTIMENT_ANALYZER = SentimentIntensityAnalyzer()


def flatten_list(l):
    """
    Recursively flattens a nested list or tuple.
    Converts each element to a string.
    """
    flattened = []
    for item in l:
        if isinstance(item, (list, tuple)):
            flattened.extend(flatten_list(item))
        else:
            flattened.append(str(item))
    return flattened


def preprocess_text(text):
    """
    Lowercase + strip punctuation.
    If input isn’t a string, flatten it first.
    """
    if not isinstance(text, str):
        text = " ".join(flatten_list(text))
    text = text.lower()
    text = re.sub(f"[{re.escape(string.punctuation)}]", "", text)
    return text


def extract_urls(text):
    """
    Find all http(s) URLs in the text.
    """
    if not isinstance(text, str):
        text = " ".join(flatten_list(text))
    return re.findall(r'(https?://\S+)', text)


def check_url_reputations(urls):
    """
    Naïve URL risk: 1 if it contains 'verify' or 'login', else 0.
    """
    reputations = []
    for url in urls:
        reputations.append(1 if ("verify" in url or "login" in url) else 0)
    return reputations


def extract_sender_info(raw_email: str) -> str:
    """
    Pull the 'From:' header if present.
    """
    if not isinstance(raw_email, str):
        raw_email = " ".join(flatten_list(raw_email))
    sender = ""
    for line in raw_email.splitlines():
        if line.lower().startswith("from:"):
            sender = line.split(":", 1)[1].strip()
            break
    return sender


def parse_headers(raw_email: str) -> dict:
    """
    Extract basic header signals: SPF/DKIM/DMARC pass flags + count of Received headers.
    """
    if not isinstance(raw_email, str):
        raw_email = " ".join(flatten_list(raw_email))
    msg = email.message_from_string(raw_email)

    # collect flags (stub logic; replace with real DNS/SPF check if desired)
    spf_pass = msg.get("Received-SPF", "").lower().startswith("pass")
    dkim_pass = "pass" in msg.get("DKIM-Signature", "").lower()
    dmarc_pass = "dmarc=pass" in msg.get("Authentication-Results", "").lower()
    received_chain = msg.get_all("Received", []) or []

    return {
        "spf_pass": int(spf_pass),
        "dkim_pass": int(dkim_pass),
        "dmarc_pass": int(dmarc_pass),
        "received_count": len(received_chain),
    }


def extract_attachments(raw_email: str) -> Counter:
    """
    Count attachments by file extension.
    """
    if not isinstance(raw_email, str):
        raw_email = " ".join(flatten_list(raw_email))
    msg = email.message_from_string(raw_email)

    exts = []
    for part in msg.walk():
        filename = part.get_filename()
        if filename and "." in filename:
            ext = filename.rsplit(".", 1)[1].lower()
            exts.append(ext)

    return Counter(exts)


def extract_features(email_text):
    """
    Build a comprehensive feature dict with:
      - cleaned_text        (for TF-IDF)
      - sentiment_compound  (VADER)
      - url_count
      - avg_url_risk
      - sender
      - spf_pass, dkim_pass, dmarc_pass, received_count
      - att_count_<ext>     for each attachment extension seen
      - text_length
    """
    # ensure flat string
    if not isinstance(email_text, str):
        email_text = " ".join(flatten_list(email_text))

    features = {}

    # 1) cleaned text
    features["cleaned_text"] = preprocess_text(email_text)

    # 2) sentiment score
    vs = SENTIMENT_ANALYZER.polarity_scores(email_text)
    features["sentiment_compound"] = vs["compound"]

    # 3) URL features
    urls = extract_urls(email_text)
    features["url_count"] = len(urls)
    reps = check_url_reputations(urls)
    features["avg_url_risk"] = sum(reps) / len(reps) if reps else 0.0

    # 4) sender info
    features["sender"] = extract_sender_info(email_text)

    # 5) header signals
    hdr = parse_headers(email_text)
    features.update(hdr)

    # 6) attachments
    atts = extract_attachments(email_text)
    for ext, cnt in atts.items():
        features[f"att_count_{ext}"] = cnt

    # 7) email length
    features["text_length"] = len(email_text)

    return features
