from datetime import datetime
from api.database import db
from werkzeug.security import generate_password_hash, check_password_hash
from typing import Any
from sqlalchemy import Table, Column, Integer, ForeignKey, String
from sqlalchemy.orm import relationship


# Association table and Role model so seed scripts and role-based APIs work
user_roles = Table(
    "user_roles",
    db.metadata,
    Column("user_id", Integer, ForeignKey("user.id"), primary_key=True),
    Column("role_id", Integer, ForeignKey("roles.id"), primary_key=True),
)


class Role(db.Model):
    __tablename__ = "roles"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)

    def __repr__(self) -> str:
        return f"<Role {self.name}>"


class User(db.Model):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    # NEW: explicit boolean column for trainer flag
    is_trainer = db.Column(db.Boolean, default=False)

    # writable relationship for role-based storage
    roles = relationship("Role", secondary=user_roles, backref="users")

    def __init__(self, username: str, password: str, is_admin: bool = False, is_trainer: bool = False) -> None:
        self.username = username
        # Accept either a raw password or a pre-hashed value. If the incoming
        # password looks like a hashed value (starts with common prefixes), store as-is.
        # Otherwise hash it so higher-level code can send either hashed seed values or raw passwd.
        if isinstance(password, str) and (
            password.startswith("pbkdf2:") or password.startswith("argon2") or password.startswith("$2b$")
        ):
            self.password = password
        else:
            self.password = generate_password_hash(password)
        self.is_admin = is_admin
        self.is_trainer = is_trainer

    def set_password(self, raw: str) -> None:
        """Convenience helper used by seed scripts."""
        self.password = generate_password_hash(raw)

    def check_password(self, plain: str) -> bool:
        if not self.password:
            return False
        try:
            return check_password_hash(self.password, plain)
        except Exception:
            return False

    # flask-login properties
    @property
    def is_active(self):
        return True

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def get_id(self):
        return str(self.id)

    def __repr__(self) -> str:
        return f"<User {self.username}>"

    # Role helper for templates and code expecting has_role
    def has_role(self, role_name: str) -> bool:
        """
        Return True if the user effectively has the given role name.
        - If a direct boolean flag exists for common roles (e.g., is_admin, is_trainer) it is respected.
        - Defensive: handles 'roles' as relationship/list/string if present.
        """
        if not role_name:
            return False

        rn = role_name.strip().lower()

        # Quick boolean shortcuts
        if rn == "admin" and getattr(self, "is_admin", False):
            return True

        if rn == "trainer" and getattr(self, "is_trainer", False):
            return True

        # Check for a relationship/attribute named 'roles'
        roles_attr: Any = getattr(self, "roles", None)
        if roles_attr is None:
            return False

        # If roles is iterable of strings or objects
        try:
            for r in roles_attr:
                if r is None:
                    continue
                if isinstance(r, str):
                    if r.strip().lower() == rn:
                        return True
                    continue
                name = None
                if hasattr(r, "name"):
                    name = getattr(r, "name")
                elif isinstance(r, dict):
                    name = r.get("name")
                if isinstance(name, str) and name.strip().lower() == rn:
                    return True
        except TypeError:
            pass

        # If roles stored as comma-separated string
        if isinstance(roles_attr, str):
            parts = [p.strip().lower() for p in roles_attr.split(",") if p.strip()]
            if rn in parts:
                return True

        return False

    def add_role(self, role):
        """
        Accept either a Role instance or a role name string.
        Ensure role exists in DB and associate it with the user.
        Keeps boolean flags in sync for common roles.
        """
        if role is None:
            return
        # resolve string -> Role row
        if isinstance(role, str):
            r = Role.query.filter_by(name=role).first()
            if r is None:
                r = Role(name=role)
                db.session.add(r)
                db.session.flush()
        else:
            r = role
        if r not in self.roles:
            self.roles.append(r)
        # keep boolean flag in sync for common roles
        if getattr(r, "name", "").lower() == "trainer":
            try:
                self.is_trainer = True
            except Exception:
                pass
        if getattr(r, "name", "").lower() == "admin":
            try:
                self.is_admin = True
            except Exception:
                pass

    def remove_role(self, role):
        """
        Remove a Role instance or role name from the user.
        Keeps boolean flags in sync for common roles.
        """
        if role is None:
            return
        if isinstance(role, str):
            r = Role.query.filter_by(name=role).first()
        else:
            r = role
        if r and r in self.roles:
            try:
                self.roles.remove(r)
            except Exception:
                pass
        # keep boolean flag in sync
        if getattr(r, "name", "").lower() == "trainer":
            try:
                self.is_trainer = False
            except Exception:
                pass
        if getattr(r, "name", "").lower() == "admin":
            try:
                self.is_admin = False
            except Exception:
                pass

    @property
    def is_trainer_effective(self) -> bool:
        """
        Canonical trainer truth across storage strategies.
        """
        if getattr(self, "is_trainer", False):
            return True
        try:
            return self.has_role("trainer")
        except Exception:
            return False
