from functools import wraps
from flask import flash, redirect, url_for
from flask_login import current_user, login_required

def admin_required(func):
    @wraps(func)
    @login_required
    def wrapper(*args, **kwargs):
        # you can customize the flash message & redirect target
        if not getattr(current_user, "is_admin", False):
            flash("You must be an administrator to access that page.", "warning")
            return redirect(url_for("home_page"))
        return func(*args, **kwargs)
    return wrapper

