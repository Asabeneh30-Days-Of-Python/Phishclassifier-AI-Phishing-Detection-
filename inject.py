from pathlib import Path

p = Path('/app/api/app.py')
bak = Path('/app/api/app.py.bak')

s = p.read_text()
marker = 'def edit_user(user_id):'
i = s.find(marker)
if i == -1:
    print('MARKER-NOT-FOUND')
    raise SystemExit(1)

ins_point = s.find('\n', i) + 1
injection = (
    '\n    # TEMP DEBUG: log incoming form items for troubleshooting\n'
    '    try:\n'
    '        current_app.logger.debug(\"REQUEST_FORM_ITEMS: %s\", list(request.form.items()))\n'
    '    except Exception:\n'
    '        current_app.logger.exception(\"Failed to log request.form items\")\n\n'
)

if not bak.exists():
    bak.write_text(s)

new = s[:ins_point] + injection + s[ins_point:]
p.write_text(new)
print('INJECTED-OK')
