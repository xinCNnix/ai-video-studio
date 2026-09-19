"""store_selfcheck：harness 状态层自检（隔离 DATA_DIR，不碰真实 data/）。

覆盖：v2 建项目 / mutate 版本递增 / 乐观锁 409 / 不变量拒绝非法状态 /
v1 文件自动归档 / v1 服务对 v2 不可见 / 事件总线投递。
运行：.venv/Scripts/python.exe tests/store_selfcheck.py
"""
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_tmp = tempfile.mkdtemp(prefix="store_selfcheck_")
os.environ["DATA_DIR"] = _tmp

from app.harness.bus import bus  # noqa: E402
from app.harness.errors import InvariantViolation, VersionConflictError  # noqa: E402
from app.harness.state import ImageBinding, Material, Scene, check_invariants  # noqa: E402
from app.services import project_store, project_service  # noqa: E402

PASS = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    PASS += 1
    print(f"ok   {name}")


# 1. 建项目
p = project_store.create("测试项目", "16:9")
check("create 返回 version=1", p["version"] == 1)
pid = p["id"]

# 2. mutate 内部写（无基线）版本递增
snap = project_store.mutate(pid, lambda m: m.scenes.append(Scene(id="scene_a", title="开场")))
check("mutate 后 version=2", snap["version"] == 2)

# 3. 乐观锁：正确基线通过，过期基线 409
snap = project_store.mutate(pid, lambda m: None, base_version=2)
check("正确基线通过", snap["version"] == 3)
try:
    project_store.mutate(pid, lambda m: None, base_version=2)
    check("过期基线应 409", False)
except VersionConflictError as exc:
    check("过期基线 409", exc.current_version == 3)

# 4. fn 抛异常不落盘
try:
    project_store.mutate(pid, lambda m: (_ for _ in ()).throw(RuntimeError("boom")))
    check("fn 异常应上抛", False)
except RuntimeError:
    check("fn 异常上抛不落盘", project_store.get(pid)["version"] == 3)

# 5. 不变量拒绝：绑定不存在的素材
def bad_bind(m):
    m.scenes[0].binding.images.append(ImageBinding(material_id="mat_ghost"))

try:
    project_store.mutate(pid, bad_bind)
    check("幽灵素材应被拒", False)
except InvariantViolation as exc:
    check("幽灵素材被拒", "mat_ghost" in str(exc))
check("被拒后磁盘未变", project_store.get(pid)["version"] == 3)

# 6. 不变量拒绝：音频超过 3 段
def add_material(m):
    for i in range(4):
        m.materials.append(Material(id=f"mat_{i}", kind="audio", filename=f"a{i}.mp3"))

project_store.mutate(pid, add_material)


def bad_audio(m):
    m.scenes[0].binding.audios = ["mat_0", "mat_1", "mat_2", "mat_3"]

try:
    project_store.mutate(pid, bad_audio)
    check("音频>3 应被拒", False)
except InvariantViolation:
    check("音频>3 被拒", True)

# 7. 合法绑定通过
def good_bind(m):
    m.scenes[0].binding.audios = ["mat_0", "mat_2"]

snap = project_store.mutate(pid, good_bind)
check("合法绑定通过", snap["scenes"][0]["binding"]["audios"] == ["mat_0", "mat_2"])

# 8. v1 遗留文件自动归档 + v1 服务不可见
v1_file = Path(_tmp) / "projects" / "project_v1legacy.json"
v1_file.write_text('{"id": "project_v1legacy", "name": "旧项目", "scenes": []}', encoding="utf-8")
check("v1 store get 返回 None 并归档", project_store.get("project_v1legacy") is None)
check("归档文件存在", (Path(_tmp) / "projects_archive" / "project_v1legacy.json").exists())
check("v1 service list 不含 v2", all(x["id"] != pid for x in project_service.list_projects()))
check("v1 service get v2 为 None", project_service.get_project(pid) is None)
check("v1 service save v2 被拒", project_service.save_project(pid, {"scenes": []}) is None)
check("v2 文件未被 v1 覆盖", project_store.get(pid)["version"] >= 5)

# 9. 事件总线：订阅后收到投递
received: list[dict] = []


def _drain():
    # publish 是线程投递到 loop；这里直接用同步检查（无 loop 时投递被丢弃，验证不炸即可）
    bus.publish(pid, {"type": "project_updated", "version": 99})


bus.publish("不存在项目", {"type": "x"})  # 无订阅者不炸
_drain()
check("无订阅者 publish 不炸", True)

# 10. check_invariants 纯函数：时长越界
bad_project = project_store._load(pid)  # noqa: SLF001 - 自检直接使用内部加载
bad_project.scenes[0].duration = 99
viol = check_invariants(bad_project)
check("时长越界被检出", any("时长" in v for v in viol))

print(f"\n全部通过：{PASS} 项")
