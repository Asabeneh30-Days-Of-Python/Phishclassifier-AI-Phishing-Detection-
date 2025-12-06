# api/intel.py
import re
import requests
import logging
import os
import time
import json
import hashlib
from urllib.parse import urlparse
from flask import current_app   # type: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

VT_API_URL = "https://www.virustotal.com/api/v3/urls"
SHORTENER_HOSTS = {"bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "is.gd", "buff.ly"}
URL_RE = re.compile(r"https?://[^\s'\"<>]+")

GSB_URL = "https://safebrowsing.googleapis.com/v4/threatMatches:find"
PHISHTANK_URL = "http://checkurl.phishtank.com/checkurl/"

def extract_urls(text: str) -> list[str]:
    if not text:
        return []
    return URL_RE.findall(text)

def _safe_head(url: str, timeout: float = 3.0) -> str:
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout)
        if r.status_code and r.url:
            return r.url
    except Exception as e:
        logger.debug("HEAD expand failed for %s: %s", url, e)
    return url

def expand_short_url(url: str, timeout: float = 3.0) -> str:
    if not url:
        return url
    host = ""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        pass
    if any(s in host for s in SHORTENER_HOSTS):
        # try safe HEAD first; fallback to GET only if configured and safe
        expanded = _safe_head(url, timeout=timeout)
        return expanded or url
    return url

def lookup_url_reputation(url: str) -> dict:
    api_key = current_app.config.get("VT_API_KEY")
    enabled = current_app.config.get("ENABLE_VT", False)
    if not api_key or not enabled:
        return {}
    headers = {"x-apikey": api_key}
    try:
        resp = requests.post(VT_API_URL, headers=headers, json={"url": url}, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        current_app.logger.warning("VT lookup failed for %s: %s", url, e)
        return {}

def _norm_url(u: str) -> str:
    p = urlparse(u, scheme="http")
    scheme = p.scheme or "http"
    host = p.hostname or ""
    path = p.path or "/"
    return f"{scheme}://{host}{path}"

def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def lookup_url_gsb(url: str, redis_client=None, cfg: dict = None) -> dict:
    """
    Google Safe Browsing Lookup helper.
    Returns compact summary:
      {"status":"ok"|"disabled"|"rate_limited"|"temporary_error"|"error",
       "threat": None or {threatType, platformType, threatEntryType},
       "checked_at": ISO8601,
       ...}
    Caches under key gsb:<sha256(norm_url)> if redis_client provided.
    """
    if cfg is None:
        cfg = {
            "ENABLE_GSB": os.environ.get("ENABLE_GSB", "0").lower() in ("1", "true", "yes", "on"),
            "GSB_API_KEY": os.environ.get("GSB_API_KEY"),
            "GSB_CLIENT": os.environ.get("GSB_CLIENT", "phishclassifier"),
            "GSB_TIMEOUT": float(os.environ.get("GSB_TIMEOUT", "3")),
            "GSB_MAX_RETRIES": int(os.environ.get("GSB_MAX_RETRIES", "3")),
            "GSB_CACHE_TTL": int(os.environ.get("GSB_CACHE_TTL", "86400")),
        }

    if not cfg.get("ENABLE_GSB") or not cfg.get("GSB_API_KEY"):
        return {"status": "disabled"}

    norm = _norm_url(url)
    cache_key = f"gsb:{_sha256_hex(norm)}"
    # Try cache
    if redis_client:
        try:
            cached = redis_client.get(cache_key)
            if cached:
                try:
                    return json.loads(cached)
                except Exception:
                    logger.exception("failed to parse cached gsb result")
        except Exception:
            logger.exception("redis read failed")

    payload = {
        "client": {"clientId": cfg.get("GSB_CLIENT"), "clientVersion": "1.0"},
        "threatInfo": {
            "threatTypes": ["MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE", "POTENTIALLY_HARMFUL_APPLICATION"],
            "platformTypes": ["ANY_PLATFORM"],
            "threatEntryTypes": ["URL"],
            "threatEntries": [{"url": norm}],
        },
    }
    params = {"key": cfg.get("GSB_API_KEY")}
    timeout = cfg.get("GSB_TIMEOUT", 3)
    max_retries = cfg.get("GSB_MAX_RETRIES", 3)

    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(GSB_URL, params=params, json=payload, timeout=timeout)
            if r.status_code == 200:
                try:
                    body = r.json()
                except Exception:
                    body = {}
                checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                if body and "matches" in body and body["matches"]:
                    m = body["matches"][0]
                    summary = {
                        "status": "ok",
                        "threat": {
                            "threatType": m.get("threatType"),
                            "platformType": m.get("platformType"),
                            "threatEntryType": m.get("threatEntryType"),
                        },
                        "checked_at": checked_at,
                    }
                else:
                    summary = {"status": "ok", "threat": None, "checked_at": checked_at}

                if redis_client:
                    try:
                        redis_client.set(cache_key, json.dumps(summary), ex=int(cfg.get("GSB_CACHE_TTL", 86400)))
                    except Exception:
                        logger.exception("redis write failed")
                return summary

            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                return {"status": "rate_limited", "retry_after": retry_after}

            if 400 <= r.status_code < 500:
                try:
                    err = r.json()
                except Exception:
                    err = {"http_status": r.status_code}
                return {"status": "error", "error": err}

            logger.warning("gsb unexpected status %s, body: %s", r.status_code, r.text[:200])

        except requests.exceptions.Timeout:
            logger.warning("gsb timeout attempt %d for %s", attempt, url)
        except requests.RequestException:
            logger.exception("gsb request exception")

        if attempt < max_retries:
            time.sleep((2 ** (attempt - 1)) + 0.1 * attempt)

    return {"status": "temporary_error", "error": "max_retries_exceeded"}

def lookup_url_phishtank(url: str, redis_client=None, cfg: dict = None) -> dict:
    """
    PhishTank lookup helper (guarded by PHISHTANK_KEY).
    Returns compact summary:
      {"status":"ok"|"error"|"temporary_error",
       "in_database": bool, "valid": bool, "phish_id": ..., "checked_at": ...}
    Caches under key phishtank:<sha256(norm_url)> when PHISHTANK_KEY is present.
    """
    if cfg is None:
        cfg = {
            "PHISHTANK_KEY": os.environ.get("PHISHTANK_KEY", "") or "",
            "PHISHTANK_TIMEOUT": float(os.environ.get("PHISHTANK_TIMEOUT", "5")),
            "PHISHTANK_MAX_RETRIES": int(os.environ.get("PHISHTANK_MAX_RETRIES", "3")),
            "PHISHTANK_TTL_SAFE": int(os.environ.get("PHISHTANK_TTL_SAFE", "86400")),
            "PHISHTANK_TTL_PHISH": int(os.environ.get("PHISHTANK_TTL_PHISH", "259200")),
        }

    if not cfg.get("PHISHTANK_KEY"):
        return {"status": "disabled"}

    norm = _norm_url(url)
    cache_key = f"phishtank:{_sha256_hex(norm)}"
    if redis_client:
        try:
            cached = redis_client.get(cache_key)
            if cached:
                try:
                    return json.loads(cached)
                except Exception:
                    logger.exception("failed to parse cached phishtank result")
        except Exception:
            logger.exception("redis read failed")

    data = {
        "url": norm,
        "format": "json",
        "app_key": cfg.get("PHISHTANK_KEY")
    }
    headers = {"User-Agent": "phishclassifier/1.0 (ops@yourdomain.example)"}
    timeout = cfg.get("PHISHTANK_TIMEOUT", 5)
    max_retries = cfg.get("PHISHTANK_MAX_RETRIES", 3)

    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(PHISHTANK_URL, data=data, headers=headers, timeout=timeout)
            if r.status_code == 200:
                try:
                    body = r.json()
                except Exception:
                    body = {}
                results = body.get("results") or {}
                res = {
                    "status": "ok",
                    "in_database": bool(results.get("in_database")),
                    "valid": bool(results.get("valid")),
                    "phish_id": results.get("phish_id"),
                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                ttl = cfg.get("PHISHTANK_TTL_PHISH") if res["in_database"] and res["valid"] else cfg.get("PHISHTANK_TTL_SAFE")
                if redis_client:
                    try:
                        redis_client.set(cache_key, json.dumps(res), ex=int(ttl))
                    except Exception:
                        logger.exception("redis write failed")
                return res
            elif 500 <= r.status_code < 600:
                logger.warning("phishtank server error %s", r.status_code)
            else:
                logger.warning("phishtank unexpected status %s %s", r.status_code, r.text[:200])
                return {"status": "error", "http_status": r.status_code, "body": r.text[:1000]}
        except requests.exceptions.Timeout:
            logger.warning("phishtank timeout attempt %d", attempt)
        except requests.RequestException:
            logger.exception("phishtank request exception")
        time.sleep(min(2 ** (attempt - 1), 10))

    return {"status": "temporary_error", "error": "max_retries_exceeded"}

def analyze_links(text: str) -> list[dict]:
    """
    Return per-link structured analysis:
    {
      url, expanded, host, is_shortener, suspicion (0.0-1.0),
      suspicion_percent, reasons: [..], vt: {...}
    }
    Controlled by ENABLE_SHORT_EXPAND and ENABLE_VT app config flags.
    """
    urls = extract_urls(text)
    out = []
    for u in urls:
        try:
            parsed = urlparse(u)
            host = (parsed.netloc or "").lower()
        except Exception:
            host = ""
        is_short = any(s in host for s in SHORTENER_HOSTS)
        expanded = u
        if current_app.config.get("ENABLE_SHORT_EXPAND", True) and is_short:
            try:
                expanded = expand_short_url(u, timeout=float(current_app.config.get("SHORT_EXPAND_TIMEOUT", 3)))
            except Exception:
                expanded = u

        vt = {}
        if current_app.config.get("ENABLE_VT", False):
            try:
                vt = lookup_url_reputation(expanded or u)
            except Exception:
                vt = {}

        reasons = []
        suspicion = 0.0

        # Local heuristics
        if is_short:
            suspicion += 0.25
            reasons.append("uses URL shortener")

        if expanded and not expanded.startswith("https://"):
            suspicion += 0.20
            reasons.append("non-HTTPS or unknown scheme")

        # numeric host (raw IP)
        try:
            h = urlparse(expanded or u).hostname or ""
            if h and all(ch.isdigit() or ch == "." for ch in h):
                suspicion += 0.25
                reasons.append("numeric host (possible raw IP)")
        except Exception:
            pass

        # suspicious TLDs
        if host and any(host.endswith(t) for t in (".tk", ".pw", ".cf", ".gq")):
            suspicion += 0.15
            reasons.append("suspicious top-level domain")

        # long URL
        if len(u) > 180:
            suspicion += 0.10
            reasons.append("very long URL")

        # VT evidence increases suspicion
        try:
            if isinstance(vt, dict) and vt.get("data"):
                # simple heuristic: presence implies possible flag
                suspicion += 0.40
                reasons.append("reputation lookup flagged URL")
        except Exception:
            pass

        suspicion = min(1.0, suspicion)
        out.append({
            "url": u,
            "expanded": expanded,
            "host": host,
            "is_shortener": is_short,
            "suspicion": round(suspicion, 3),
            "suspicion_percent": round(suspicion * 100.0, 1),
            "reasons": reasons,
            "vt": vt or {}
        })
    return out
