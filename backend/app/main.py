"""FastAPI 入口：AI Video Studio Backend。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .api import agent as agent_api
from .api import files as files_api
from .api import image as image_api
from .api import jobs as jobs_api
from .api import projects as projects_api
from .api import projects_v2 as projects_v2_api
from .api import prompt as prompt_api
from .api import settings as settings_api
from .api import video as video_api
from .api import voice as voice_api


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 重启恢复：续上重启前已提交、云端仍在生成的视频任务（轮询+下载+写回分镜）
    from .services import job_recovery

    try:
        n = await job_recovery.recover_interrupted_jobs()
        if n:
            print(f"[startup] 已恢复 {n} 个重启前提交的视频任务，继续轮询云端")
    except Exception as exc:  # noqa: BLE001 - 恢复失败不阻断启动
        print(f"[startup] 视频任务恢复异常：{exc}")
    # 看门狗启动巡检：服务重启造成的悬空 running pipeline 转 paused（可从检查点续跑）
    try:
        from .harness.watchdog import reap_stale_pipelines

        stale = reap_stale_pipelines()
        if stale:
            print(f"[startup] 已把 {len(stale)} 个中断项目的流水线转为 paused，可续跑")
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] 流水线巡检异常：{exc}")
    yield


app = FastAPI(title=settings.APP_NAME, version=settings.APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in (
    settings_api.router,
    prompt_api.router,
    image_api.router,
    voice_api.router,
    video_api.router,
    jobs_api.router,
    projects_api.router,
    projects_v2_api.router,
    agent_api.router,
    files_api.router,
):
    app.include_router(router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": settings.APP_VERSION}
