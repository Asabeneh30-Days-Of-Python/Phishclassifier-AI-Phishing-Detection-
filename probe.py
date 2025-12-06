from api.app import create_app
app = create_app()
print("created app", getattr(app, "import_name", None))
from api.reports import QuarantinedEmail
ctx = app.app_context()
ctx.push()
print("QuarantinedEmail available:", hasattr(QuarantinedEmail, "query"))
try:
    q = QuarantinedEmail.query.limit(1).all()
    print("query ran, rows:", len(q))
except Exception as e:
    import traceback
    print("query failed:", repr(e))
    traceback.print_exc()
ctx.pop()