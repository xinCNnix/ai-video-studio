"""离线自检：MiniMax 音色设计（/v1/voice_design）适配器与 API 链路。

不依赖真实 Key：MockTransport 拦截 HTTP，校验请求体/响应解析/重试/错误映射，
并端到端验证 设计 → 试听音频固化资产 → minimax_voice_id → resolve_tts_voice
三厂商路由（MiniMax 走永久 voice_id，小米走样本克隆）。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.providers.base import ProviderError
from app.providers.tts import minimax_tts as mm_mod
from app.providers.tts.minimax_tts import MiniMaxTTS

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{f'  {detail}' if detail and not cond else ''}")
    if not cond:
        FAILS.append(name)


def patch_async_client(handler):
    """把 httpx.AsyncClient 换成强制注入 MockTransport 的子类，返回还原函数。"""
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    class PatchedClient(real):
        def __init__(self, *a, **kw):
            kw.pop("transport", None)
            super().__init__(*a, transport=transport, **kw)

    httpx.AsyncClient = PatchedClient
    return lambda: setattr(httpx, "AsyncClient", real)


def ok_body(voice_id="ttv-voice-test", audio_hex="fffb9044"):
    return {
        "voice_id": voice_id,
        "trial_audio": audio_hex,
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }


async def adapter_checks() -> None:
    mm = MiniMaxTTS("k-test", base_url="https://api.minimax.cn")
    reqs: list[httpx.Request] = []

    def ok_handler(request: httpx.Request) -> httpx.Response:
        reqs.append(request)
        return httpx.Response(200, json=ok_body())

    restore = patch_async_client(ok_handler)
    try:
        vid, audio = await mm.design_voice("低沉磁性的中年男声", "你好，试听。")
        check("设计成功返回 voice_id", vid == "ttv-voice-test")
        check("试听音频 hex 解码", audio == bytes.fromhex("fffb9044"))
        r = reqs[-1]
        check("请求路径 /v1/voice_design", r.url.path == "/v1/voice_design", str(r.url))
        body = json.loads(r.content)
        check("请求体带 prompt", body.get("prompt") == "低沉磁性的中年男声")
        check("请求体带 preview_text", body.get("preview_text") == "你好，试听。")
        check("缺省不传 voice_id", "voice_id" not in body)

        await mm.design_voice("描述", "试听", voice_id="vga_custom1")
        body = json.loads(reqs[-1].content)
        check("指定 voice_id 透传", body.get("voice_id") == "vga_custom1")

        await mm.design_voice("描述", "字" * 600)
        body = json.loads(reqs[-1].content)
        check("preview_text 截断 500", len(body.get("preview_text", "")) == 500)
    finally:
        restore()

    def biz_err(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"base_resp": {"status_code": 1008, "status_msg": "余额不足"}})

    restore = patch_async_client(biz_err)
    try:
        try:
            await mm.design_voice("d", "p")
            check("业务错误抛 ProviderError", False)
        except ProviderError as exc:
            check("业务错误抛 ProviderError", "余额不足" in str(exc), str(exc))
    finally:
        restore()

    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="rate limit")
        return httpx.Response(200, json=ok_body())

    restore = patch_async_client(flaky)
    try:
        vid, _ = await mm.design_voice("d", "p")
        check("429 限流重试后成功", vid == "ttv-voice-test" and calls["n"] == 2, f"calls={calls['n']}")
    finally:
        restore()

    def no_audio(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"voice_id": "v", "base_resp": {"status_code": 0}})

    restore = patch_async_client(no_audio)
    try:
        try:
            await mm.design_voice("d", "p")
            check("缺试听音频报错", False)
        except ProviderError:
            check("缺试听音频报错", True)
    finally:
        restore()


async def api_checks() -> None:
    from fastapi import HTTPException

    from app.api import voice as voice_api
    from app.config import voice_assets
    from app.config.voice_assets import resolve_tts_voice
    from app.providers import registry as registry_mod

    mm = MiniMaxTTS("k-test", base_url="https://api.minimax.cn")

    def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_body("ttv-voice-e2e", "fffb9044"))

    real_get = registry_mod.get_minimax_tts
    registry_mod.get_minimax_tts = lambda credential_id=None: mm
    restore = patch_async_client(ok_handler)
    created_id: str | None = None
    try:
        asset = await voice_api.design_minimax_voice(
            voice_api.DesignMinimaxRequest(name="测试设计音色", prompt="低沉磁性男声", preview_text="自定义试听文本")
        )
        created_id = asset["id"]
        check("端到端返回资产", asset.get("id", "").startswith("voice_"))
        check("资产记录 minimax_voice_id", asset.get("minimax_voice_id") == "ttv-voice-e2e")
        check("试听音频固化为样本", bool(asset.get("has_sample")) and asset.get("kind") == "clone")
        check("ref_text=试听文本（供 Higgs 克隆）", asset.get("ref_text") == "自定义试听文本")
        raw = voice_assets.find_asset(created_id) or {}
        sample = voice_assets.SAMPLES_DIR / raw.get("sample", "不存在")
        check("样本文件落盘且内容正确", sample.exists() and sample.read_bytes() == bytes.fromhex("fffb9044"), str(sample))
        check("desc 留档设计描述", raw.get("desc") == "低沉磁性男声")
        check(
            "MiniMax 合成路由到永久 voice_id",
            resolve_tts_voice("minimax", "speech-2.6-hd", created_id) == ("speech-2.6-hd", "ttv-voice-e2e"),
        )
        m, v = resolve_tts_voice("xiaomi", "mimo-v2.5-tts", created_id)
        check("小米合成路由到样本克隆", m == "mimo-v2.5-tts-voiceclone" and v.startswith("data:audio/"), f"{m} {v[:30]}")
        check("默认试听文本合规", 0 < len(voice_api.DEFAULT_DESIGN_PREVIEW) <= 500)

        def auth_err(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"base_resp": {"status_code": 1004, "status_msg": "鉴权失败"}})

        restore()
        restore = patch_async_client(auth_err)
        try:
            await voice_api.design_minimax_voice(
                voice_api.DesignMinimaxRequest(name="x", prompt="x", preview_text="x")
            )
            check("适配器错误映射 502", False)
        except HTTPException as exc:
            check("适配器错误映射 502", exc.status_code == 502 and "鉴权失败" in str(exc.detail), f"{exc.status_code} {exc.detail}")
    finally:
        restore()
        registry_mod.get_minimax_tts = real_get
        if created_id:
            voice_assets.delete_asset(created_id)  # 清理：样本与资产记录一并删除


async def main() -> None:
    real_delays = mm_mod._RETRY_DELAYS
    mm_mod._RETRY_DELAYS = (0.05,)  # 重试等待缩短，离线测试不空转
    try:
        await adapter_checks()
        await api_checks()
    finally:
        mm_mod._RETRY_DELAYS = real_delays
    print(f"\n{'=' * 40}\n{'ALL PASS' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
