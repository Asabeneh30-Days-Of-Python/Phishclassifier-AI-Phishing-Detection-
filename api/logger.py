# api/logger.py

import logging
import logging.config    # explicitly import the ‘config’ submodule

# Attempt to import the Fluentd handler; if it’s not installed, fall back gracefully
try:
    from fluent.handler import FluentHandler  # type: ignore[reportMissingImports]
    HAVE_FLUENT = True
except ImportError:
    HAVE_FLUENT = False


def setup_logging():
    """
    Configure Python logging with:
      - a console (StreamHandler)
      - an optional FluentHandler if fluent-logger is installed
    """
    # Build the list of active handlers
    active_handlers = ["console"]
    if HAVE_FLUENT:
        active_handlers.append("fluent")

    # Centralized dictConfig for all handlers/formatters
    LOGGING_CONFIG = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)s %(name)s: %(message)s"
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
            },
            # Only configure 'fluent' if the class is available
            "fluent": {
                "class": "fluent.handler.FluentHandler",  # Pylance now sees the module path
                "formatter": "default",
                "tag": "phishclassifier",
            },
        },
        "root": {
            "level": "INFO",
            "handlers": active_handlers,
        },
    }

    # Apply configuration
    logging.config.dictConfig(LOGGING_CONFIG)
    return logging.getLogger()
