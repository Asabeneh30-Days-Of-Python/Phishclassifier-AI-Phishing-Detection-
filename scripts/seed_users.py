# scripts/seed_users.py

import os, sys

# ───────────────────────────────────────────────────────────────────────────────
# Make sure the project root is on PYTHONPATH
# ───────────────────────────────────────────────────────────────────────────────
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from api.app import create_app
from api.database import db
from models.user import User, Role

app = create_app()

with app.app_context():
    # 1) Recreate the entire schema from scratch
    db.drop_all()
    db.create_all()

    # 2) Create roles
    admin_r   = Role(name="admin")
    trainer_r = Role(name="trainer")
    db.session.add_all([admin_r, trainer_r])
    db.session.commit()

    # 3) Admin-only user
    u_admin = User(username="admin", password="AdminPass!123", is_admin=True)
    u_admin.set_password("AdminPass!123")
    u_admin.roles.append(admin_r)

    # 4) Trainer-only user
    u_trainer = User(username="trainer", password="TrainerPass!123", is_admin=False)
    u_trainer.set_password("TrainerPass!123")
    u_trainer.add_role(trainer_r)

    # 5) Admin + Trainer user
    u_both = User(username="admin_trainer", password="AdminTrainerPass!123", is_admin=True)
    u_both.set_password("AdminTrainerPass!123")
    u_both.add_role(admin_r)
    u_both.add_role(trainer_r)

    # 6) Save them all
    db.session.add_all([u_admin, u_trainer, u_both])
    db.session.commit()

    print("✅ Seeded users:")
    print("   • admin           / AdminPass!123       (admin-only)")
    print("   • trainer         / TrainerPass!123     (trainer-only)")
    print("   • admin_trainer   / AdminTrainerPass!123 (admin+trainer)")