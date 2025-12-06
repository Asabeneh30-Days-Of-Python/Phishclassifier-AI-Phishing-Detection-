# api/datasets.py

import os
import glob
import mailbox
import json
import random
import logging
from pathlib import Path
from typing import List, Tuple, Optional

# --------------------------------------------------------------------
# 1) Sample Dataset
# --------------------------------------------------------------------
def load_sample_dataset() -> Tuple[List[str], List[int]]:
    """
    Built-in sample dataset: 7 ham (0), 10 phishing (1).
    """
    emails = [
        "This is a normal email from a friend.",
        "Your recent order from our store has been shipped.",
        "Please find attached the monthly newsletter with updates.",
        "Meeting rescheduled to 3 PM tomorrow, please confirm attendance.",
        "Your account security update has been completed successfully.",
        "We are excited to share our annual report with you.",
        "Your subscription has been renewed successfully.",
        # phishing
        "Congratulations, you have won a free iPhone! Click here now.",
        "This email contains a phishing scam with a malicious link.",
        "Reminder: Your account updates are needed; click the link to verify.",
        "Click here to claim your prize immediately.",
        "Urgent: Your account has been compromised, please verify immediately.",
        "Your account will be closed unless you verify immediately.",
        "Suspicious activity detected. Click here to secure your account.",
        "Due to suspicious activity, your account will be suspended unless verified immediately.",
        "Immediate action is required! Verify your account now to avoid closure.",
        "Alert: Multiple failed login attempts detected on your account. Click here to secure it."
    ]
    labels = [0] * 7 + [1] * 10
    return emails, labels


# --------------------------------------------------------------------
# 2) Enron Dataset (ham & phishing)
# --------------------------------------------------------------------
def load_enron_dataset(limit: Optional[int] = None
                      ) -> Tuple[List[str], List[int]]:
    """
    Walks the Enron maildir tree under:
      1) ENRON_DATA_DIR (if set), else
      2) repo-root/datasets/enron/maildir
    Labels any folder named 'sent*' as phishing.
    """

    # 1) Use explicit env-override if given
    env_dir = os.getenv("ENRON_DATA_DIR")
    if env_dir:
        base = Path(env_dir).resolve()
    else:
        # 2) Otherwise derive from this file's location
        base = (Path(__file__).parent
                         .parent   # move up from .../api to repo root
                         / "datasets"
                         / "enron"
                         / "maildir"
               ).resolve()

    texts: List[str] = []
    labels: List[int] = []
    ignore_dirs = {
        "calendar", "contacts", "folders_to_export",
        "journal", "notes", "discussion_threads", "tasks"
    }

    if not base.is_dir():
        logging.warning(f"Enron base directory not found: {base}")
        return texts, labels

    for root, dirs, files in os.walk(base, followlinks=False):
        folder = os.path.basename(root).lower()
        if folder in ignore_dirs:
            continue

        label = 1 if folder.startswith("sent") else 0

        for fname in files:
            path = os.path.join(root, fname)
            try:
                raw = open(path, "r", encoding="utf-8", errors="ignore") \
                          .read() \
                          .strip()
            except Exception:
                continue

            if not raw:
                continue

            texts.append(raw)
            labels.append(label)

            if limit is not None and len(texts) >= limit:
                logging.info(f"⟳ EXIT after {len(texts)} msgs (limit={limit})")
                return texts, labels

    logging.info(f"⟳ EXIT full scan: {len(texts)} messages")
    return texts, labels


# --------------------------------------------------------------------
# 3) SpamAssassin Public Corpus
# --------------------------------------------------------------------
def load_spamassassin_dataset() -> Tuple[List[str], List[int]]:
    """
    Loads SpamAssassin Public Corpus:
      easy_ham + hard_ham → 0, spam → 1
    """
    base = os.path.join("datasets", "spamassassin")
    emails: List[str] = []
    labels: List[int] = []

    for subdir, label in [("easy_ham", 0), ("hard_ham", 0), ("spam", 1)]:
        pattern = os.path.join(base, subdir, "*")
        for filepath in glob.glob(pattern):
            try:
                text = open(filepath, errors="ignore").read().strip()
            except Exception:
                continue
            if text:
                emails.append(text)
                labels.append(label)

    return emails, labels


# --------------------------------------------------------------------
# 4) PhishTank Dataset
# --------------------------------------------------------------------
def load_phishtank_dataset() -> Tuple[List[str], List[int]]:
    """
    Loads PhishTank JSON (all phishing=1).
    Supports both dict format with "urls" key and raw list of entries.
    """
    path = os.path.join("datasets", "phishtank", "phishtank.json")
    if not os.path.exists(path):
        return [], []

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    entries = data.get("urls", data) if isinstance(data, dict) else data
    emails: List[str] = []
    labels: List[int] = []

    for entry in entries:
        url = entry.get("url", "").strip() if isinstance(entry, dict) else str(entry).strip()
        if url:
            emails.append(f"Please verify your account at {url}")
            labels.append(1)

    return emails, labels


# --------------------------------------------------------------------
# 5) Real Enterprise Logs Loader
# --------------------------------------------------------------------
ENTERPRISE_DIR = os.getenv(
    "ENTERPRISE_LOG_DIR",
    os.path.join(os.path.dirname(__file__), "../data/enterprise_logs")
)

def load_enterprise_dataset(limit: Optional[int] = None) -> Tuple[List[str], List[int]]:
    """
    Loads raw enterprise .txt logs from ENTERPRISE_DIR.
    Filenames starting with 'phish_' → label 1, others → label 0.
    """
    texts: List[str] = []
    labels: List[int] = []
    pattern = os.path.join(ENTERPRISE_DIR, "*.txt")

    for path in glob.glob(pattern):
        name = os.path.basename(path)
        label = 1 if name.lower().startswith("phish_") else 0
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read().strip()
        except Exception:
            continue

        if txt:
            texts.append(txt)
            labels.append(label)
            if limit is not None and len(texts) >= limit:
                break

    return texts, labels


# --------------------------------------------------------------------
# 6) Synthetic (Rule-Based) Enterprise Loader
# --------------------------------------------------------------------
def load_synthetic_enterprise_dataset(
    n_examples: int = 1000,
    phish_ratio: float = 0.3
) -> Tuple[List[str], List[int]]:
    """
    Generates a synthetic enterprise corpus via templates.
    """
    names      = ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank"]
    services   = ["VPN account", "email mailbox", "payroll system", "HR portal"]
    phish_urls = [
        "http://secure-company.verify-login.com",
        "https://account-update.company-it.com",
        "http://office-support.reset-password.net"
    ]

    clean_templates = [
        "Hi {name},\n\nQuarterly metrics are attached. Let me know if you have questions.\n\nCheers,\nManagement",
        "Hello {name},\n\nThe cafeteria menu for next week is live. Enjoy!\n\nThanks,\nOffice Admin",
        "Dear {name},\n\nPlease review the attached team roster updates.\n\nBest,\nHR"
    ]

    phish_templates = [
        "Dear {name},\n\nWe detected unauthorized access to your {service}. "
        "Please click {url} immediately to secure your account.\n\nIT Security Team",
        "Hi {name},\n\nYour {service} password is expiring today. "
        "Reset it here: {url}\n\nRegards,\nSupport Desk"
    ]

    texts: List[str] = []
    labels: List[int] = []
    for _ in range(n_examples):
        if random.random() < phish_ratio:
            tpl   = random.choice(phish_templates)
            label = 1
            text  = tpl.format(
                name    = random.choice(names),
                service = random.choice(services),
                url     = random.choice(phish_urls)
            )
        else:
            tpl   = random.choice(clean_templates)
            label = 0
            text  = tpl.format(name=random.choice(names))

        texts.append(text)
        labels.append(label)

    return texts, labels


# --------------------------------------------------------------------
# 7) LLM-Generated Enterprise Loader
# --------------------------------------------------------------------
SYNTH_JSON = os.path.join(
    os.path.dirname(__file__),
    "../data/enterprise_synth.json"
)

def load_llm_enterprise_dataset(limit: Optional[int] = None) -> Tuple[List[str], List[int]]:
    """
    Reads LLM-generated synthetic enterprise emails from JSON.
    Expects a list of {"text": "...", "label": 0|1}.
    """
    if not os.path.exists(SYNTH_JSON):
        return [], []

    with open(SYNTH_JSON, "r", encoding="utf-8") as f:
        arr = json.load(f)

    if limit:
        arr = arr[:limit]

    texts  = [item["text"]  for item in arr]
    labels = [item["label"] for item in arr]
    return texts, labels


# --------------------------------------------------------------------
# 8) Hybrid Real + Synthetic Loader
# --------------------------------------------------------------------
def load_hybrid_enterprise_dataset(
    real_limit: Optional[int]  = None,
    synth_count: Optional[int] = None,
    phish_ratio: float         = 0.3
) -> Tuple[List[str], List[int]]:
    """
    Combines real enterprise logs with synthetic examples.
    """
    real_texts, real_labels = load_enterprise_dataset(limit=real_limit)

    target_synth = synth_count if synth_count is not None else max(len(real_texts), 1000)
    synth_texts, synth_labels = load_synthetic_enterprise_dataset(
        n_examples  = target_synth,
        phish_ratio = phish_ratio
    )

    texts  = real_texts  + synth_texts
    labels = real_labels + synth_labels
    return texts, labels
