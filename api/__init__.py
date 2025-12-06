# app/__init__.py

import os
from flask import Flask

def create_app():
    app = Flask(__name__, instance_relative_config=False)

    # 1) Load your config.py settings
    env = os.environ.get("FLASK_ENV", "development").lower()
    if env == "production":
        app.config.from_object("config.ProductionConfig")
    else:
        app.config.from_object("config.DevelopmentConfig")

    # 2) Initialize extensions
    # from .extensions import db, login_manager
    # db.init_app(app)
    # login_manager.init_app(app)

    # 3) Register blueprints
    # from .views import main_bp
    # app.register_blueprint(main_bp)

    return app
