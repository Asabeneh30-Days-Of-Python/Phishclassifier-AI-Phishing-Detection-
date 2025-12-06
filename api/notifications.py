# api/notifications.py
from datetime import datetime
from flask import Blueprint, request, render_template, redirect, url_for, flash
from flask_login import login_required
from api.database import db
from decorators import admin_required

notifications_bp = Blueprint("notifications", __name__, url_prefix="/admin")


class NotificationRule(db.Model):
    __tablename__ = "notification_rules"

    id             = db.Column(db.Integer, primary_key=True)
    name           = db.Column(db.String(100), nullable=False, unique=True)
    condition_type = db.Column(
        db.Enum("volume_spike", "high_risk", "custom_json", name="notif_cond"),
        nullable=False
    )
    threshold      = db.Column(db.Float, nullable=False)
    active         = db.Column(db.Boolean, default=True, nullable=False)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, onupdate=datetime.utcnow)

    def __init__(
        self,
        name: str,
        condition_type: str,
        threshold: float,
        active: bool = True
    ):
        self.name = name
        self.condition_type = condition_type
        self.threshold = threshold
        self.active = active


@notifications_bp.route("/notifications")
@login_required
@admin_required
def list_notifications():
    rules = NotificationRule.query.order_by(NotificationRule.id.desc()).all()
    return render_template("notifications.html", rules=rules)


@notifications_bp.route("/notifications/create", methods=["GET", "POST"])
@login_required
@admin_required
def create_notification():
    if request.method == "POST":
        form = request.form
        rule = NotificationRule(
            name=form["name"],
            condition_type=form["condition_type"],
            threshold=float(form["threshold"]),
            active=("active" in form)
        )
        db.session.add(rule)
        db.session.commit()
        flash("Notification rule created", "success")
        return redirect(url_for("notifications.list_notifications"))

    # GET → show empty form
    return render_template("notifications.html", rules=[], form_action="create")


@notifications_bp.route("/notifications/edit/<int:rule_id>", methods=["GET", "POST"])
@login_required
@admin_required
def edit_notification(rule_id):
    rule = NotificationRule.query.get_or_404(rule_id)

    if request.method == "POST":
        form = request.form
        rule.name           = form["name"]
        rule.condition_type = form["condition_type"]
        rule.threshold      = float(form["threshold"])
        rule.active         = ("active" in form)
        db.session.commit()
        flash("Notification rule updated", "success")
        return redirect(url_for("notifications.list_notifications"))

    # GET → show form pre-populated
    return render_template(
        "notifications.html",
        rules=NotificationRule.query.all(),
        edit_rule=rule
    )


@notifications_bp.route("/notifications/delete/<int:rule_id>", methods=["POST"])
@login_required
@admin_required
def delete_notification(rule_id):
    rule = NotificationRule.query.get_or_404(rule_id)
    db.session.delete(rule)
    db.session.commit()
    flash("Notification rule deleted", "warning")
    return redirect(url_for("notifications.list_notifications"))
