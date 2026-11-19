"""Write .do/app.rendered.yaml: .do/app.yaml with the app's current encrypted secret
values (EV[...]) copied in, so `doctl apps update` doesn't wipe them.

    .venv/bin/python .do/render_spec.py <app-id>        # existing app
    .venv/bin/python .do/render_spec.py --new           # first create: prompts for secrets

Uses doctl. SECRET_KEY is generated if the app has none. A secret set in the environment
(e.g. ANTHROPIC_API_KEY=... python .do/render_spec.py <app-id>) replaces the app's value.
"""

import getpass
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SECRETS = ["DATABASE_URL", "APP_PASSWORD", "SECRET_KEY", "RESEND_API_KEY", "RESEND_WEBHOOK_SECRET", "ANTHROPIC_API_KEY"]


def current(app_id):
    out = subprocess.run(["doctl", "apps", "spec", "get", app_id, "--format", "json"], check=True,
                         capture_output=True, text=True).stdout
    spec = json.loads(out)
    vals = {}
    envs = list(spec.get("envs", []))
    for comp in spec.get("services", []) + spec.get("workers", []) + spec.get("jobs", []):
        envs += comp.get("envs", [])
    for e in envs:
        if e.get("type") == "SECRET" and e.get("value"):
            vals.setdefault(e["key"], e["value"])
    return vals


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    vals = {} if sys.argv[1] == "--new" else current(sys.argv[1])
    for k in SECRETS:
        if os.environ.get(k):
            vals[k] = os.environ[k]
        elif k not in vals:
            if k == "SECRET_KEY":
                vals[k] = secrets.token_urlsafe(48)
            else:
                vals[k] = getpass.getpass(f"{k} (empty to leave unset): ")
    text = (HERE / (os.environ.get("SPEC") or "app.yaml")).read_text()
    for k, v in vals.items():
        if not v:
            continue
        text = re.sub(r"\{ key: %s, scope: (\w+), type: SECRET \}" % k,
                      lambda m: '{ key: %s, scope: %s, type: SECRET, value: "%s" }' % (k, m.group(1), v.replace('"', '\\"')),
                      text)
    out = HERE / "app.rendered.yaml"
    if "type: SECRET }" in text:
        print("warning: some secrets have no value:", re.findall(r"key: (\w+), scope: \w+, type: SECRET \}", text))
    out.write_text(text)
    out.chmod(0o600)
    print(f"wrote {out} (contains secrets; delete after use)")


if __name__ == "__main__":
    main()
