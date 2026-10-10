"""离线自检：剧本正文上限统一（MAX_SCRIPT_CHARS）与截断可见性。

背景（2026-10-02 第十回事故）：上传预览卡 60000、intake 入库 200000，三处不一致——
106,274 字符的 14 镜稿被上传预览静默截到 60,000，agent 拆出 9 镜照常走完全程。
修复后契约：
  1. doc 上传返回正文上限 = intake 入库上限 = doc 落盘重读上限 = screenplay.source_text
     上限 = config.settings.MAX_SCRIPT_CHARS（单一事实来源）
  2. 超 61k 的旧事故场景：全文返回、text_truncated=False（60000 时代会被截）
  3. 超 MAX_SCRIPT_CHARS：截到上限且 text_truncated=True，绝不静默
"""
import os
import sys
import tempfile
from pathlib import Path

# 隔离数据目录：上传会落盘，不能写进真实 data/
_TMP = tempfile.mkdtemp(prefix="script_limit_selfcheck_")
os.environ["PROJECT_ROOT"] = _TMP
os.environ["DATA_DIR"] = str(Path(_TMP) / "data")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import files as files_api
from app.config import settings


def _upload_doc(client: TestClient, name: str, text: str) -> dict:
    resp = client.post(
        "/api/uploads",
        data={"kind": "doc"},
        files={"file": (name, text.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def main() -> int:
    failures: list[str] = []

    # 契约 0：四处引用同一常量（stages 侧再 import 校验，防将来改散）
    from app.services.pipeline import stages
    if stages.MAX_SCRIPT_CHARS != settings.MAX_SCRIPT_CHARS:
        failures.append(f"stages.MAX_SCRIPT_CHARS={stages.MAX_SCRIPT_CHARS} 与 settings 不一致")
    if settings.MAX_SCRIPT_CHARS != 500_000:
        failures.append(f"MAX_SCRIPT_CHARS={settings.MAX_SCRIPT_CHARS}，应为 500000")

    app = FastAPI()
    app.include_router(files_api.router)
    client = TestClient(app)

    # 契约 1：第十回事故场景复现——106,274 字符全文返回，不再截到 60000
    ch10 = Path(__file__).resolve().parents[2] / "data" / "uploads" / "doc"
    orig = None
    if ch10.exists():
        v3 = sorted(ch10.glob("*第十回*v3*.md"))
        if v3:
            orig = v3[0].read_text(encoding="utf-8")
    if orig and len(orig) > 60_000:
        r = _upload_doc(client, "tenth.md", orig)
        if r.get("text") != orig:
            failures.append(f"事故剧本未全文返回：len(text)={len(r.get('text') or '')} != {len(orig)}")
        if r.get("text_truncated") is not False:
            failures.append(f"106k 稿不应截断，text_truncated={r.get('text_truncated')}")
    else:
        # 环境里没有原稿时用等长合成文本覆盖同一断言
        synthetic = "## 镜头01｜测试\n\n" + "字" * 106_264
        r = _upload_doc(client, "tenth.md", synthetic)
        if r.get("text") != synthetic or r.get("text_truncated") is not False:
            failures.append(f"106k 合成稿断言失败：truncated={r.get('text_truncated')}")

    # 契约 2：超上限截断且必须可见
    over = "镜" * (settings.MAX_SCRIPT_CHARS + 1000)
    r = _upload_doc(client, "over.md", over)
    if len(r.get("text") or "") != settings.MAX_SCRIPT_CHARS:
        failures.append(f"超长稿应截到 {settings.MAX_SCRIPT_CHARS}，实际 {len(r.get('text') or '')}")
    if r.get("text_truncated") is not True:
        failures.append(f"超长稿 text_truncated 应为 True，实际 {r.get('text_truncated')}")

    # 契约 3：小文档不受影响、不误报
    r = _upload_doc(client, "small.md", "# 小剧本\n\n正文")
    if r.get("text") != "# 小剧本\n\n正文" or r.get("text_truncated") is not False:
        failures.append(f"小文档断言失败：{r}")

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print(f"PASS: MAX_SCRIPT_CHARS={settings.MAX_SCRIPT_CHARS}，"
          f"事故剧本(106k)全文返回，超限截断可见，小文档不受影响")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
