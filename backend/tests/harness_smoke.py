"""harness 冒烟：p2 API + trace + watchdog（隔离 DATA_DIR）。"""
import json
import os
import sys
import tempfile
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="harness_smoke_")
warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def step(name):
    print(f"--> {name}", flush=True)


step("导入完成")
c = TestClient(app)
r = c.post("/api/p2/projects", json={"name": "冒烟"})
pid = r.json()["id"]
step(f"create {pid}")

r = c.post(f"/api/p2/projects/{pid}/scenes", json={"base_version": 1})
sid = r.json()["scenes"][0]["id"]
step(f"add scene {sid}")

r = c.delete(f"/api/p2/projects/{pid}/scenes/{sid}")
step(f"del scene {r.status_code}")

r = c.patch(f"/api/p2/projects/{pid}", json={"base_version": 3, "settings": {"video_model": "minimax_h3_zm_u24"}})
step(f"settings {r.status_code} {r.json()['settings']['video_model']}")

step("SSE 开始")
with c.stream("GET", f"/api/p2/projects/{pid}/events") as s:
    got = None
    for line in s.iter_lines():
        step(f"sse line: {line[:60]!r}")
        if line.startswith("data:"):
            got = json.loads(line[5:])
            break
    print("sse snapshot:", got["type"], got["version"], flush=True)
step("SSE 结束")

from app.harness.trace import RunTrace, list_runs, read_trace  # noqa: E402

t = RunTrace("run_test_1", pid, "pipeline", goal="测试")
t.log("stage_start", stage="intake")
t.end("done")
print("trace events:", len(read_trace("run_test_1")), "| runs:", [x["run_id"] for x in list_runs(project_id=pid)], flush=True)

from app.harness.watchdog import watchdog  # noqa: E402

print("acquire1:", watchdog.try_acquire(pid, "run_a", "pipeline"), flush=True)
print("acquire2(应False):", watchdog.try_acquire(pid, "run_b", "pipeline"), flush=True)
watchdog.release(pid, "run_a")
print("release后acquire:", watchdog.try_acquire(pid, "run_b", "pipeline"), flush=True)
watchdog.release(pid, "run_b")
print("SMOKE OK", flush=True)
