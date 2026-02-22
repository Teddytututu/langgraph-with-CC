"""Poll a task status until completion."""
import urllib.request
import json
import time
import sys

task_id = sys.argv[1] if len(sys.argv) > 1 else "bfab0e38"
url = f"http://127.0.0.1:8001/api/tasks/{task_id}"

for i in range(120):
    try:
        r = urllib.request.urlopen(url, timeout=5)
        t = json.loads(r.read())
    except Exception as e:
        print(f"[{i*5:3d}s] fetch error: {e}")
        time.sleep(5)
        continue

    subtasks = t.get("subtasks", [])
    sub_str = ", ".join(
        f"{s['title'][:18]}:{s['status']}" for s in subtasks
    ) if subtasks else "(none yet)"

    status = t.get("status", "?")
    print(f"[{i*5:3d}s] status={status:10s}  subtasks=[{sub_str}]")

    if status in ("completed", "failed"):
        print("\n=== FINISHED ===")
        if t.get("result"):
            print("RESULT (first 800 chars):")
            print(t["result"][:800])
        if t.get("error"):
            print("ERROR:", t["error"])
        break

    time.sleep(5)
else:
    print("Timed out after 10 minutes.")
