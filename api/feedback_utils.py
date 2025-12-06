# api/feedback_utils.py
ALLOWED_FEEDBACK = {"none", "false_positive", "false_negative"}

def normalize_feedback(value):
    """
    Return a canonical feedback string. Invalid, empty, or None -> 'none'.
    """
    try:
        if value is None:
            return "none"
        v = str(value).strip()
        return v if v in ALLOWED_FEEDBACK else "none"
    except Exception:
        return "none"
