"""
Celery beat schedule snippet to include into your Celery configuration.
Drop into Celery app config or import from tasks and apply to celery.conf.beat_schedule.

Example usage in your Celery setup:
    from celery import Celery
    import celerybeat_schedule
    app = Celery(...)
    app.conf.beat_schedule.update(celerybeat_schedule.BEAT_SCHEDULE)
"""

BEAT_SCHEDULE = {
    "purge-old-reports-daily": {
        "task": "reports.purge_old_reports",
        "schedule": 24 * 60 * 60,  # run once per day (seconds)
        "args": (30,),  # delete reports older than 30 days
        "options": {"queue": "maintenance"}
    }
}
