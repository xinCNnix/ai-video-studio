"""编辑命令冒烟：改时长同步缩放 Prompt 时间轴 + 素材池重排（隔离 DATA_DIR，不联网）。

覆盖：
- patch_scene duration：Action Timeline 链式整数缩放、总时长对齐、## 原文保真块不动
- patch_scene 同时提交 prompt+duration：以提交值为基准缩放
- materials reorder：排列校验（400）与实际交换；script 混排位置不受影响
运行：.venv/Scripts/python.exe tests/edit_timing_reorder_check.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="edit_check_")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.projects_v2 import router  # noqa: E402
from app.harness.state import Material, Scene, new_material_id, new_scene_id  # noqa: E402
from app.services import project_store  # noqa: E402

PASS = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    PASS += 1
    print(f"ok   {name}")


app = FastAPI()
app.include_router(router)
client = TestClient(app)


def make_project(with_scene_prompt: str | None = None, duration: int = 10) -> tuple[str, str, int]:
    snap = project_store.create("check", "16:9")
    pid = snap["id"]
    sid = new_scene_id()
    project_store.mutate(pid, lambda p: p.scenes.append(
        Scene(id=sid, title="镜1", duration=duration, prompt=with_scene_prompt or "")))
    return pid, sid, snap["version"]


# ---------------- 时长 → Prompt 时间轴同步 ----------------
pid, sid, _ = make_project("总述。\n## Action Timeline\n0-3s: 走入诊室。\n3-8s: 检查。\n8-10s: 总结。")
snap = client.get(f"/api/projects/{pid}").json()
out = client.patch(f"/api/projects/{pid}/scenes/{sid}",
                   json={"duration": 6, "base_version": snap["version"]}).json()
s = out["scenes"][0]
check("duration 更新", s["duration"] == 6)
check("timeline 链式缩放", "0-2s" in s["prompt"] and "2-5s" in s["prompt"] and "5-6s" in s["prompt"], s["prompt"])

# ## 原文 保真块：时间引用同步缩放，台词/描述文字逐字不动
pid, sid, _ = make_project("0-10s 总述。\n\n## 原文（逐字保留）\n片段1｜0-10s 原文 <Image 1>")
snap = client.get(f"/api/projects/{pid}").json()
out = client.patch(f"/api/projects/{pid}/scenes/{sid}",
                   json={"duration": 4, "base_version": snap["version"]}).json()
s = out["scenes"][0]
check("非原文部分缩放", s["prompt"].startswith("0-4s 总述。"), s["prompt"])
check("原文块时间引用同步缩放（残留旧时长会借'以本节为准'盖掉正确参数）",
      "片段1｜0-4s 原文 <Image 1>" in s["prompt"] and "## 原文（逐字保留）" in s["prompt"], s["prompt"])

# 原文块内行首时间轴 + 块边界游标重置（正文时间轴与原文时间轴是两条 0 起点序列）
pid, sid, _ = make_project("Action Timeline\n0-5s: A。\n5-10s: B。\n\n## 原文\n视频长度：10秒\n0.0–3.0s｜Audio 1\n3.0–10.0s｜Audio 2")
snap = client.get(f"/api/projects/{pid}").json()
out = client.patch(f"/api/projects/{pid}/scenes/{sid}",
                   json={"duration": 5, "base_version": snap["version"]}).json()
s = out["scenes"][0]
check("正文时间轴缩放且总和对齐 5s", "0-3s" in s["prompt"] and "3-5s" in s["prompt"], s["prompt"])
check("原文块总时长引用对齐", "视频长度：5秒" in s["prompt"], s["prompt"])
check("原文块时间轴从 0 重算（不接正文游标）", "0–2s｜Audio 1" in s["prompt"] and "2–5s｜Audio 2" in s["prompt"], s["prompt"])

# 同时提交 prompt + duration：以提交值为基准缩放（基准 = 修改前时长）
pid, sid, _ = make_project(duration=8)
snap = client.get(f"/api/projects/{pid}").json()
out = client.patch(f"/api/projects/{pid}/scenes/{sid}",
                   json={"prompt": "0-8s 内容。", "duration": 4, "base_version": snap["version"]}).json()
check("显式 prompt 随 duration 缩放", out["scenes"][0]["prompt"] == "0-4s 内容。", out["scenes"][0]["prompt"])

# ---------------- 素材池重排 ----------------
snap = project_store.create("reorder-check", "16:9")
pid2 = snap["id"]
mids = {}


def add_mats(p):
    for key, kind, fn in [("script", "script", "inline.txt"), ("img1", "image", "a_图一.png"),
                          ("aud1", "audio", "b_音一.mp3"), ("img2", "image", "c_图二.png"),
                          ("img3", "image", "d_图三.png")]:
        mid = new_material_id()
        mids[key] = mid
        p.materials.append(Material(id=mid, kind=kind, url=None if kind == "script" else f"/files/{fn}",
                                    filename=fn, text="剧本" if kind == "script" else None))


project_store.mutate(pid2, add_mats)
snap = client.get(f"/api/projects/{pid2}").json()
names0 = [m["filename"] for m in snap["materials"]]
check("初始顺序", names0 == ["inline.txt", "a_图一.png", "b_音一.mp3", "c_图二.png", "d_图三.png"], names0)

# 非法排列 → 400
r = client.post(f"/api/projects/{pid2}/materials/reorder",
                json={"order": [mids["img1"], mids["aud1"]], "base_version": snap["version"]})
check("非法排列 400", r.status_code == 400, r.text)

# 交换 图一 与 图三（中间隔着音频/图二）
snap = client.get(f"/api/projects/{pid2}").json()
new_order = [mids["script"], mids["img3"], mids["aud1"], mids["img2"], mids["img1"]]
r = client.post(f"/api/projects/{pid2}/materials/reorder",
                json={"order": new_order, "base_version": snap["version"]})
check("重排 200", r.status_code == 200, r.text)
names = [m["filename"] for m in r.json()["materials"]]
check("池顺序已交换", names == ["inline.txt", "d_图三.png", "b_音一.mp3", "c_图二.png", "a_图一.png"], names)

print(f"\n{PASS} 项全绿")
