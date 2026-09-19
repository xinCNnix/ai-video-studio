"""复现脚本：在隔离 DATA_DIR 下跑一轮 agent，抓 LLM 每步原始输出。
不烧生成额度：video/image/tts 任务全部用假 runner 立即置 completed。
只真实调用 LLM（与线上同一凭据/模型解析逻辑）。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_ROOT.parent
os.environ["DATA_DIR"] = str(REPO_ROOT / "repro_data")
sys.path.insert(0, str(BACKEND_ROOT))

from app.services import agent_service, image_service, video_service, voice_service  # noqa: E402
from app.jobs import job_manager  # noqa: E402

LOG = REPO_ROOT / "repro" / "llm_trace.log"
LOG.parent.mkdir(exist_ok=True)


def _log(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


# ---- 劫持三个生成 runner：立即完成，零外部消费 ----
async def _fake_image(job_id, **kw):
    job_manager.update(job_id, status="completed", output="/fake/image.png")
    return {"url": "/fake/image.png"}


async def _fake_video(job_id, **kw):
    job_manager.update(job_id, status="completed", output="/fake/video.mp4", duration_seconds=5.0)
    return {"url": "/fake/video.mp4"}


async def _fake_voice(job_id, **kw):
    job_manager.update(job_id, status="completed", output="/fake/voice.mp3", duration_seconds=3.5)
    return {"url": "/fake/voice.mp3"}


image_service.run_image_job = _fake_image
video_service.run_video_job = _fake_video
voice_service.run_voice_job = _fake_voice

# ---- 劫持 LLM：记录每次原始输出 ----
_real_resolve = agent_service.resolve_llm_call


class _LlmWrapper:
    def __init__(self, inner):
        self._inner = inner
        self.calls = 0

    async def generate(self, messages, model, **kw):
        self.calls += 1
        _log(f"\n{'='*70}\n[LLM CALL #{self.calls}] model={model} msgs={len(messages)} "
             f"in_tokens_est={sum(len(str(m.get('content',''))) for m in messages)//2}")
        raw = await self._inner.generate(messages, model, **kw)
        _log(f"[LLM CALL #{self.calls} RAW len={len(raw or '')}]\n<<<BEGIN>>>\n{raw}\n<<<END>>>")
        return raw


def _wrapped_resolve(credential_id, provider_hint=None, model=None):
    llm, m = _real_resolve(credential_id, provider_hint=provider_hint, model=model)
    return _LlmWrapper(llm), m


agent_service.resolve_llm_call = _wrapped_resolve
agent_service.MAX_STEPS = 10  # 控成本：足够暴露 2-3 步后的行为

# 复刻真实项目分镜作为页面上下文（前端 _run_context 会带当前场景）
_proj = json.load(open(REPO_ROOT / "repro_data" / "projects" / "project_repro01.json", encoding="utf-8"))
_s0 = _proj["scenes"][0]
CONTEXT = {
    "page": "/studio",
    "scene_title": _s0.get("title") or "",
    "description": _s0.get("description") or "",
    "prompt": (_s0.get("prompt") or "")[:200],
    "narration": _s0.get("narration") or "",
    "image": _s0.get("image") or "",
    "aspect_ratio": _proj.get("aspect_ratio") or "16:9",
    "project_id": "project_repro01",
}
GOAL = "我给了分镜提示词、参考图、配音，生成分镜视频"


async def main():
    steps_seen = []

    def on_step(step):
        steps_seen.append(step)
        head = json.dumps(step, ensure_ascii=False, default=str)[:400]
        print(f"STEP {step.get('step')}: type={step.get('type')} tool={step.get('tool')} :: {head}", flush=True)

    result = await agent_service.run_agent(GOAL, CONTEXT, None, None, history=None, on_step=on_step)
    print("\n===== FINAL =====")
    print("reply:", str(result.get("reply"))[:1500])
    print("jobs:", result.get("jobs"))
    print("steps:", [(s.get("type"), s.get("tool")) for s in result.get("steps", [])])


if __name__ == "__main__":
    if LOG.exists():
        LOG.unlink()
    asyncio.run(main())
