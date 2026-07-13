"""Shared OAuth callback HTML template — suyo dark theme."""

import json


def _js_embed(value: object) -> str:
    """Serialize a value to a JS literal safe to embed inside <script>.

    ``json.dumps`` handles quoting/escaping of the value itself; we then
    neutralise the sequences that could break out of the surrounding
    ``<script>`` element or terminate the JS string context:

    * ``<`` / ``>`` — prevent ``</script>`` (and comment) breakout.
    * ``&`` — prevent HTML-entity based smuggling.
    * U+2028 / U+2029 — valid in JSON strings but illegal raw in JS,
      would otherwise let an attacker inject a line terminator.
    """
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_callback(
    success: bool,
    *,
    message_type: str = "connector-auth-complete",
    extra_data: dict | None = None,
) -> str:
    """Render a styled OAuth callback page.

    Args:
        success: Whether the auth flow succeeded.
        message_type: postMessage type for the opener window.
        extra_data: Additional key/value pairs to include in postMessage.
    """
    title = "Connected" if success else "Failed"
    msg = (
        "Authorization successful. This window will close automatically."
        if success
        else "Authorization failed. Please try again."
    )
    icon = "&#10003;" if success else "&#10007;"
    color = "#34d399" if success else "#f87171"

    # Build the postMessage payload as a real object and serialize it
    # safely. ``message_type`` and ``extra_data`` may carry values that
    # originate from OAuth callback query parameters (e.g. ``state``),
    # so they must never be interpolated into the <script> verbatim.
    pm_payload: dict[str, object] = {"type": message_type, "success": success}
    if extra_data:
        pm_payload.update(extra_data)
    pm_object = _js_embed(pm_payload)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>suyo — {title}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: #0a0a0a;
    color: #e5e5e5;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
  }}
  .card {{
    text-align: center;
    max-width: 360px;
    padding: 48px 32px;
  }}
  .icon {{
    width: 56px;
    height: 56px;
    border-radius: 50%;
    background: {color}15;
    border: 1.5px solid {color}40;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 24px;
    color: {color};
    margin-bottom: 20px;
  }}
  h1 {{
    font-size: 18px;
    font-weight: 600;
    margin-bottom: 8px;
    color: #f5f5f5;
  }}
  p {{
    font-size: 13px;
    color: #a3a3a3;
    line-height: 1.5;
    margin-bottom: 24px;
  }}
  .hint {{
    font-size: 11px;
    color: #525252;
  }}
</style>
</head>
<body>
<div class="card">
  <div class="icon">{icon}</div>
  <h1>{title}</h1>
  <p>{msg}</p>
  <p class="hint">This window will close automatically.</p>
</div>
<script>
  if (window.opener) {{
    window.opener.postMessage({pm_object}, "*");
    setTimeout(() => window.close(), 1500);
  }}
</script>
</body>
</html>"""
