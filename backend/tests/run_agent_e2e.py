"""Agent 端到端回归测试（无需真实 LLM / 网络）。

用脚本化的 FakeLLM 驱动 run_agent 完整循环，覆盖历史上出过问题的路径：
  1. 批量配音：script 原文逐字透传、按行拆分条数正确
  2. 断点接续：重复句子跳过、新句子提交
  3. 坏格式 JSON（缺 tool 字段）自动恢复
  4. 手动停止：cancel 后抛 ProviderError
  5. 混合输出（说明文字 + 内嵌工具 JSON）处理
  6. 模型输出带真实换行的 JSON 仍可解析（曾导致"只回复不执行"）
  7. 循环防御：纯文本宣言回喂、顽固纯文本降级、键序容错、多内嵌工具、同错熔断、取消隔离、空 summary 合成
  8. 一键出片：无关键帧契约拦截；有旁白自动合成配音后提交；无旁白按静音镜提交（零弹回）
用法：.venv/Scripts/python.exe tests/run_agent_e2e.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.jobs import job_manager  # noqa: E402
from app.services import agent_service, voice_service  # noqa: E402
from app.providers.base import ProviderError  # noqa: E402

PASS, FAIL = [], []


class FakeLLM:
    """按脚本依次返回预设输出，模拟模型行为。"""

    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.calls = 0

    async def generate(self, messages, model, **kw):
        await asyncio.sleep(0.05)  # 留出时间让 cancel 测试能介入
        self.calls += 1
        if self.calls <= len(self.outputs):
            return self.outputs[self.calls - 1]
        return '{"action":"finish","summary":"测试结束"}'


async def fake_voice_job(job_id, provider, model, credential_id, text, voice, speed, emotion):
    jm = job_manager
    jm.update(job_id, status="generating")
    await asyncio.sleep(0.05)
    url = f"/api/files/outputs/audio/fake_{job_id}.mp3"
    jm.update(job_id, status="completed", output=url, outputs=[url], duration_seconds=1.0)


def patch_llm(outputs):
    llm = FakeLLM(outputs)
    agent_service.resolve_llm_call = lambda *a, **k: (llm, "fake-model")
    return llm


def tool_call(tool: str, args: dict) -> str:
    return json.dumps({"action": "tool", "tool": tool, "args": args}, ensure_ascii=False)


async def run(goal, context=None):
    return await agent_service.run_agent(goal, context or {"page": "/"}, None, None)


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS" if cond else "  FAIL"), name, detail if not cond else "")


async def wait_jobs_done(jobs: list[dict], timeout: float = 30.0):
    """等待一批任务全部到终态（错峰提交每条间隔 3s）。"""
    waited = 0.0
    while waited < timeout:
        states = [job_manager.get(j["job_id"])["status"] for j in jobs]
        if all(s in ("completed", "failed") for s in states):
            return states
        await asyncio.sleep(1.0)
        waited += 1.0
    return [job_manager.get(j["job_id"])["status"] for j in jobs]


async def test_batch_voice_verbatim():
    print("[1] 批量配音：script 逐字透传 + 按行拆分")
    script = "第一行原文。\n第二行原文！\n第三行……原文？"
    patch_llm([
        tool_call("generate_voices_batch", {"script": script}),
        '{"action":"finish","summary":"完成"}',
    ])
    res = await run("只配音：" + script)
    check("提交条数 = 行数(3)", len(res["jobs"]) == 3, f"got {len(res['jobs'])}")
    texts = [str(job_manager.get(j["job_id"])["params"].get("text")) for j in res["jobs"]]
    expect = ["第一行原文。", "第二行原文！", "第三行……原文？"]
    check("文本逐字一致", texts == expect, str(texts))
    states = await wait_jobs_done(res["jobs"])
    check("全部任务完成", all(s == "completed" for s in states), str(states))
    for j in res["jobs"]:
        job_manager.delete(j["job_id"])


async def test_resume_skip():
    print("[2] 断点接续：重复句子跳过")
    script = "断点测试独有句子A。\n断点测试独有句子B。"
    patch_llm([tool_call("generate_voices_batch", {"script": script}), '{"action":"finish","summary":"完成"}'])
    res1 = await run("只配音：" + script)
    await wait_jobs_done(res1["jobs"])
    script2 = script + "\n断点测试独有句子C。"
    patch_llm([tool_call("generate_voices_batch", {"script": script2}), '{"action":"finish","summary":"完成"}'])
    res2 = await run("再配一次：" + script2)
    check("第二次仅提交 1 条新任务", len(res2["jobs"]) == 1, f"got {len(res2['jobs'])}")
    check("沿用 2 条旧任务", len(res2.get("voice_reused", [])) == 2, str(res2.get("voice_reused")))
    for j in res1["jobs"] + res2["jobs"]:
        job_manager.delete(j["job_id"])


async def test_malformed_recovers():
    print("[3] 坏格式 JSON（缺 tool 字段）自动恢复")
    patch_llm([
        '{"action":"tool","args":{"text":"坏格式"}}',
        tool_call("set_narration", {"text": "恢复后的旁白"}),
        '{"action":"finish","summary":"完成"}',
    ])
    res = await run("设置旁白")
    tools = [s.get("tool") for s in res["steps"] if s.get("type") == "tool"]
    check("未执行未知工具", all(t for t in tools), str(tools))
    check("恢复后正常执行 set_narration", "set_narration" in tools)
    check("updates 正确", res["updates"].get("narration") == "恢复后的旁白")


async def test_cancel():
    print("[4] 手动停止")
    patch_llm([
        tool_call("set_narration", {"text": "第一步"}),
        tool_call("set_narration", {"text": "第二步"}),
        '{"action":"finish","summary":"完成"}',
    ])

    async def cancel_soon():
        await asyncio.sleep(0.08)
        agent_service.cancel_run()

    try:
        await asyncio.gather(run("随便做点什么"), cancel_soon())
        check("cancel 抛出 ProviderError", False, "未抛出")
    except ProviderError as e:
        check("cancel 抛出 ProviderError", "手动停止" in str(e), str(e)[:60])


async def test_embedded_tools():
    print("[5] 混合输出（说明文字 + 内嵌工具 JSON）")
    raw = '好的，我来设置旁白：{"action":"tool","tool":"set_narration","args":{"text":"内嵌旁白"}} 请稍等'
    patch_llm([raw, '{"action":"finish","summary":"完成"}'])
    res = await run("设置旁白")
    check("内嵌工具被执行", res["updates"].get("narration") == "内嵌旁白", str(res["updates"]))


async def test_raw_newline_json():
    print("[6] 模型输出带真实换行的 JSON 仍可解析")
    # 模拟常见情况：值内是真实换行而不是 \n 转义（曾导致整体解析失败、被当成纯文本回复）
    raw = '{"action":"tool","tool":"set_narration","args":{"text":"第一行\n第二行"}}'
    from app.services.agent_service import _extract_json

    d = _extract_json(raw)
    check("真实换行被修复并解析", d.get("action") == "tool" and d.get("tool") == "set_narration", str(d)[:80])
    patch_llm([raw, '{"action":"finish","summary":"完成"}'])
    res = await run("设置旁白")
    check("带换行的工具调用被执行", res["updates"].get("narration") == "第一行\n第二行", str(res["updates"]))


async def test_structured_lines():
    print("[7] 结构化 lines：text 逐字 + 音色/情绪字段 + 中文情绪映射")
    from app.services.agent_service import _norm_emotion

    check("中文情绪映射 开心→happy", _norm_emotion("开心") == "happy", str(_norm_emotion("开心")))
    check("中文情绪映射 悲伤→sad", _norm_emotion("悲伤") == "sad")
    check("英文直通 neutral", _norm_emotion("neutral") == "neutral")
    check("未识别返回 None", _norm_emotion("神秘情绪") is None)

    lines = [
        {"text": "结构化第一行原文。", "voice": "male-qn-jingying", "emotion": "惊讶"},
        {"text": "结构化第二行原文！", "emotion": "sad"},
        {"text": "结构化第三行……原文？"},
    ]
    patch_llm([
        tool_call("generate_voices_batch", {"lines": lines}),
        '{"action":"finish","summary":"完成"}',
    ])
    res = await run("只配音（结构化）")
    check("提交条数 = lines 数(3)", len(res["jobs"]) == 3, f"got {len(res['jobs'])}")
    jobs = [job_manager.get(j["job_id"]) for j in res["jobs"]]
    p0, p1, p2 = (j["params"] for j in jobs)
    check("文本逐字", [j["params"]["text"] for j in jobs] == ["结构化第一行原文。", "结构化第二行原文！", "结构化第三行……原文？"])
    check("音色/情绪进参数", p0.get("voice") == "male-qn-jingying" and p0.get("emotion") == "surprised", str(p0))
    check("缺省音色/情绪兜底", p2.get("emotion") == "neutral", str(p2))
    for j in res["jobs"]:
        job_manager.delete(j["job_id"])


async def test_script_pipe_annotation():
    print("[8] script 行内标注：文本|音色|情绪")
    script = "标注第一行。|male-qn-jingying|开心\n标注第二行，无标注。"
    patch_llm([
        tool_call("generate_voices_batch", {"script": script}),
        '{"action":"finish","summary":"完成"}',
    ])
    res = await run("只配音（标注）")
    jobs = [job_manager.get(j["job_id"]) for j in res["jobs"]]
    texts = [j["params"]["text"] for j in jobs]
    check("竖线标注被剥离", texts == ["标注第一行。", "标注第二行，无标注。"], str(texts))
    check("标注解析为参数", jobs[0]["params"].get("voice") == "male-qn-jingying" and jobs[0]["params"].get("emotion") == "happy", str(jobs[0]["params"]))
    for j in res["jobs"]:
        job_manager.delete(j["job_id"])


async def test_force_regenerate():
    print("[9] force=true：禁用沿用，全部重新生成")
    script = "重做测试独有句子X。"
    patch_llm([tool_call("generate_voices_batch", {"script": script}), '{"action":"finish","summary":"完成"}'])
    res1 = await run("只配音：" + script)
    await wait_jobs_done(res1["jobs"])
    patch_llm([tool_call("generate_voices_batch", {"script": script, "force": True}), '{"action":"finish","summary":"完成"}'])
    res2 = await run("重新配一次：" + script)
    check("force 后仍然提交（不沿用）", len(res2["jobs"]) == 1 and res2["jobs"][0]["job_id"] != res1["jobs"][0]["job_id"])
    check("voice_reused 为空", not res2.get("voice_reused"), str(res2.get("voice_reused")))
    for j in res1["jobs"] + res2["jobs"]:
        job_manager.delete(j["job_id"])



async def test_empty_and_duplicate_lines():
    print("[10] 空文本行被过滤 + 重复行各提交")
    patch_llm([
        tool_call("generate_voices_batch", {"lines": [{"text": "   "}, {"text": "有效句子。"}]}),
        '{"action":"finish","summary":"完成"}',
    ])
    res = await run("只配音")
    check("空文本行被过滤", len(res["jobs"]) == 1, f"got {len(res['jobs'])}")
    for j in res["jobs"]:
        job_manager.delete(j["job_id"])

    patch_llm([
        tool_call("generate_voices_batch", {"script": "重复台词。\n重复台词。"}),
        '{"action":"finish","summary":"完成"}',
    ])
    res2 = await run("只配音（重复）")
    check("重复行各提交一条", len(res2["jobs"]) == 2, f"got {len(res2['jobs'])}")
    ids = [j["job_id"] for j in res2["jobs"]]
    check("重复行任务 ID 不同", len(set(ids)) == 2)
    for j in res2["jobs"]:
        job_manager.delete(j["job_id"])


async def fake_image_job(job_id, provider, model, credential_id, prompt, size, n, references):
    await asyncio.sleep(0.05)
    url = f"/api/files/outputs/image/fake_{job_id}.png"
    job_manager.update(job_id, status="completed", output=url, outputs=[url])


async def fake_video_job(job_id, provider, model, credential_id, mode, prompt, duration,
                         aspect_ratio, resolution, images, last_images, videos, audios, options):
    await asyncio.sleep(0.05)
    url = f"/api/files/outputs/video/fake_{job_id}.mp4"
    job_manager.update(job_id, status="completed", output=url, outputs=[url])


async def test_image_and_scene_video():
    print("[11] 生图/生视频链路（假 Provider）")
    from app.services import image_service, video_service, project_service
    from app.config import settings

    orig_img, orig_vid = image_service.run_image_job, video_service.run_video_job
    image_service.run_image_job = fake_image_job
    video_service.run_video_job = fake_video_job
    proj_id = None
    try:
        # 1) 无 Prompt 生图 → 清晰报错，不提交任务
        patch_llm([tool_call("generate_image", {}), '{"action":"finish","summary":"完成"}'])
        res = await run("生成图片")
        r = next(s["result"] for s in res["steps"] if s.get("type") == "tool")
        check("无 Prompt 生图被拒", not r.get("ok") and "缺少图片 Prompt" in r.get("error", ""), str(r))

        # 2) 有 Prompt 生图 → 提交 image 任务并完成
        patch_llm([tool_call("generate_image", {"prompt": "一只蓝色小精灵"}), '{"action":"finish","summary":"完成"}'])
        res = await run("生成图片")
        img_jobs = [j for j in res["jobs"] if j["type"] == "image"]
        check("生图任务已提交", len(img_jobs) == 1)
        await wait_jobs_done(res["jobs"])
        check("生图任务完成", job_manager.get(img_jobs[0]["job_id"])["status"] == "completed")
        for j in res["jobs"]:
            job_manager.delete(j["job_id"])

        # 3) 分镜流：建项目 → create_scenes → 无关键帧拦截（契约预检先于配音）→ 出图 →
        #    一键出片：自动合成旁白 + 提交视频
        proj = project_service.create_project("e2e测试项目", "16:9")
        proj_id = proj["id"]
        scenes_payload = [
            {"title": f"分镜{i}", "description": "d", "prompt": f"prompt {i} 0-2s 动作", "narration": f"第{i}镜旁白", "duration": 5, "gen_mode": "i2v", "keyframe_prompt": f"完整关键帧描述：中景，角色小墨姿态{i}，暖光"}
            for i in range(3)
        ]
        patch_llm([
            tool_call("create_scenes", {"characters": [{"name": "小墨", "design": "蓝色圆脸小精灵"}], "scenes": scenes_payload}),
            tool_call("generate_scene_video", {"index": 0}),
            tool_call("generate_scene_image", {"index": 0}),
            tool_call("generate_scene_video", {"index": 0}),
            '{"action":"finish","summary":"完成"}',
        ])
        res = await run("做分镜视频", context={"page": "/studio", "project_id": proj_id, "aspect_ratio": "16:9"})
        results = [s["result"] for s in res["steps"] if s.get("type") == "tool" and isinstance(s.get("result"), dict)]
        check("create_scenes 成功", any(r.get("ok") and r.get("scene_count") == 3 for r in results), str(results[:1]))
        check("分镜数据完整落库", any(r.get("ok") and r.get("scene_count") == 3 for r in results))
        r_vid_reject = next((r for r in results if not r.get("ok") and "关键帧" in str(r.get("error", ""))), None)
        check("i2v 无关键帧被拒并提示", r_vid_reject is not None, str(results))
        r_img = next((r for r in results if r.get("ok") and r.get("image")), None)
        check("分镜素材图已生成写入", r_img is not None)
        r_shot = next((r for r in results if r.get("ok") and r.get("job_id") and "自动合成旁白" in str(r.get("note", ""))), None)
        check("一键出片自动合成旁白后提交视频", r_shot is not None, str([r.get("note") for r in results]))
        vid_jobs = [j for j in res["jobs"] if j["type"] == "video"]
        # 交接流水线会把模型没提交完的分镜全部出片（模型只做了分镜 0，1/2 由流水线补齐）
        check("全部分镜视频任务均已提交（模型 1 镜 + 交接补齐 2 镜）",
              len(vid_jobs) == 3 and {j.get("scene_index") for j in vid_jobs} == {0, 1, 2}, str(res["jobs"]))
        proj_mid = project_service.get_project(proj_id)
        check("旁白已由一键出片写入分镜", proj_mid["scenes"][0].get("voice") is not None)
        # 校验角色设定卡被注入 Prompt
        proj_after = project_service.get_project(proj_id)
        injected = sum(1 for s in proj_after["scenes"] if "Character Reference" in s["prompt"])
        check("角色设定卡注入全部分镜", injected == 3, f"{injected}/3")
        for j in res["jobs"]:
            job_manager.delete(j["job_id"])
    finally:
        image_service.run_image_job = orig_img
        video_service.run_video_job = orig_vid
        if proj_id:
            import os
            try:
                os.unlink(settings.PROJECTS_DIR / f"{proj_id}.json")
            except OSError:
                pass



async def test_scene_append_batches():
    print("[12] 长剧分批：append 追加 + 上限 120")
    from app.services import project_service
    from app.config import settings
    proj = project_service.create_project("e2e长剧项目", "16:9")
    pid = proj["id"]
    try:
        def batch(start, n, append):
            scenes = [
                {"title": f"镜{start+i}", "description": "d", "prompt": f"p{start+i} 0-2s", "narration": "n", "duration": 6, "gen_mode": "i2v", "keyframe_prompt": "kf"}
                for i in range(n)
            ]
            args = {"characters": [{"name": "主角", "design": "红色斗篷少年"}], "scenes": scenes}
            if append:
                args["append"] = True
            return tool_call("create_scenes", args)

        patch_llm([
            batch(1, 20, False),   # 第一批 20 镜
            batch(21, 20, True),   # 第二批追加 20 镜
            batch(41, 5, True),    # 第三批追加 5 镜
            '{"action":"finish","summary":"完成"}',
        ])
        res = await run("做长剧分镜", context={"page": "/studio", "project_id": pid, "aspect_ratio": "16:9"})
        proj_after = project_service.get_project(pid)
        check("三批累计 45 镜", len(proj_after["scenes"]) == 45, f"got {len(proj_after['scenes'])}")
        check("角色卡合并不重复", len(proj_after.get("characters", [])) == 1, str(proj_after.get("characters")))
        titles = [s["title"] for s in proj_after["scenes"]]
        check("追加顺序正确（镜45 在最后）", titles[-1] == "镜45" and titles[0] == "镜1", str(titles[:2] + titles[-1:]))

        # 上限：>120 拒绝
        patch_llm([
            tool_call("create_scenes", {"scenes": [{"title": "x", "prompt": "p", "duration": 5}] * 201}),
            '{"action":"finish","summary":"完成"}',
        ])
        res2 = await run("超限测试", context={"page": "/studio", "project_id": pid})
        r = next(s["result"] for s in res2["steps"] if s.get("type") == "tool")
        check("超过 200 镜被拒", not r.get("ok") and "200" in r.get("error", ""), str(r))
    finally:
        import os
        try:
            os.unlink(settings.PROJECTS_DIR / f"{pid}.json")
        except OSError:
            pass


class RepeatLLM:
    """永远返回同一输出的模型（模拟顽固畸形输出）。"""

    def __init__(self, output: str):
        self.output = output
        self.calls = 0

    async def generate(self, messages, model, **kw):
        self.calls += 1
        return self.output


async def test_plain_text_preamble_recovers():
    print("[13] 纯文本宣言被回喂纠错而不是当作回复结束（线上 bug 回归）")
    patch_llm([
        "我先查看项目当前状态，了解已有的分镜、配音和参考图情况。",
        tool_call("query_jobs", {}),
        '{"action":"finish","summary":"完成"}',
    ])
    res = await run("继续生成")
    check("宣言没有被当作最终回复", res["reply"] == "完成", res["reply"][:50])
    tools = [s.get("tool") for s in res["steps"] if s.get("type") == "tool"]
    check("纠错后工具正常执行", tools == ["query_jobs"], str(tools))


async def test_stubborn_plain_text_degrades():
    print("[13b] 顽固纯文本：多次纠错后优雅降级为回复，不无限打转")
    llm = RepeatLLM("我先看看项目情况。")
    agent_service.resolve_llm_call = lambda *a, **k: (llm, "fake-model")
    res = await run("继续生成")
    check("降级回复含警示与原始文本",
          "未输出可执行动作" in res["reply"] and "我先看看项目情况。" in res["reply"], res["reply"][:60])
    check("调用次数有上限", llm.calls == 3, str(llm.calls))


async def test_key_order_tolerant():
    print("[13c] 工具 JSON 键序颠倒/带换行缩进仍可解析执行")
    raw = '好的。{\n  "args": {"text": "乱序旁白"},\n  "tool": "set_narration",\n  "action": "tool"\n}'
    patch_llm([raw, '{"action":"finish","summary":"完成"}'])
    res = await run("设置旁白")
    check("乱序键的内嵌工具被执行", res["updates"].get("narration") == "乱序旁白", str(res["updates"]))


async def test_multi_embedded_all_executed():
    print("[13d] 一条输出里的多个内嵌工具全部按序执行")
    raw = (
        "我先查任务再看项目状态："
        '{"action":"tool","tool":"query_jobs","args":{}} 和 '
        '{"action":"tool","tool":"get_project_state","args":{}}'
    )
    patch_llm([raw, '{"action":"finish","summary":"完成"}'])
    res = await run("多工具")
    tools = [s.get("tool") for s in res["steps"] if s.get("type") == "tool"]
    check("两个内嵌工具都执行", tools == ["query_jobs", "get_project_state"], str(tools))


async def test_same_error_circuit_breaker():
    print("[13e] 同一工具相同错误连续失败触发熔断，强制收尾")
    llm = RepeatLLM(tool_call("noop_不存在的工具", {}))
    agent_service.resolve_llm_call = lambda *a, **k: (llm, "fake-model")
    res = await run("反复调用坏工具")
    check("步数被熔断（≤4 次失败）", llm.calls <= 5, str(llm.calls))
    check("回复说明熔断原因", "连续" in res["reply"] and "noop_不存在的工具" in res["reply"], res["reply"][:80])


async def test_cancel_isolation():
    print("[13f] 取消隔离：cancel 只影响当时在跑的 run，之后启动的新 run 不受影响")
    llm = FakeLLM([tool_call("query_jobs", {})] * 6)  # 连续工具调用，留出取消窗口
    agent_service.resolve_llm_call = lambda *a, **k: (llm, "fake-model")
    task_a = asyncio.create_task(run("长任务A"))
    await asyncio.sleep(0.12)
    agent_service.cancel_run()
    res_b = await run("新任务B")
    check("取消后启动的新 run 正常完成", res_b["reply"] == "测试结束", res_b["reply"][:40])
    try:
        await task_a
        check("被取消的 run 抛 ProviderError", False, "未抛出")
    except ProviderError as e:
        check("被取消的 run 抛 ProviderError", "手动停止" in str(e), str(e)[:60])


async def test_empty_summary_synthesizes():
    print("[13g] finish summary 两次为空：合成含已执行工具的兜底总结")
    patch_llm([
        tool_call("query_jobs", {}),
        '{"action":"finish","summary":" "}',
        '{"action":"finish","summary":""}',
    ])
    res = await run("查询任务")
    check("不落到'任务已执行'兜底", res["reply"] != "任务已执行", res["reply"])
    check("兜底总结包含已执行工具", "query_jobs" in res["reply"], res["reply"])


async def test_voice_first_guard():
    print("[14] 一键出片：有旁白镜自动配音后提交；无旁白镜按静音镜直接提交")
    from app.services import project_service, image_service, video_service
    from app.config import settings

    orig_img, orig_vid = image_service.run_image_job, video_service.run_video_job
    image_service.run_image_job = fake_image_job
    video_service.run_video_job = fake_video_job
    proj = project_service.create_project("e2e旁白先行", "16:9")
    pid = proj["id"]
    try:
        patch_llm([
            tool_call("create_scenes", {"force": True, "scenes": [
                {"title": "s0", "prompt": "p0 0-2s", "narration": "测试旁白文案一句", "duration": 5, "gen_mode": "i2v", "keyframe_prompt": "完整关键帧描述：中景，角色站在阳光下，暖光"},
                {"title": "s1", "prompt": "p1 0-2s", "narration": "", "duration": 5, "gen_mode": "i2v", "keyframe_prompt": "完整关键帧描述：特写，花瓣飘落，柔光"},
            ]}),
            tool_call("generate_scene_image", {"index": 0}),
            tool_call("generate_scene_image", {"index": 1, "reference_index": 0}),
            tool_call("generate_scene_video", {"index": 0}),
            tool_call("generate_scene_video", {"index": 1}),
            '{"action":"finish","summary":"完成"}',
        ])
        res = await run("做视频", context={"page": "/studio", "project_id": pid, "aspect_ratio": "16:9"})
        results = [s["result"] for s in res["steps"] if s.get("type") == "tool" and isinstance(s.get("result"), dict)]
        shot_ok = [r for r in results if r.get("ok") and r.get("job_id")]
        check("两次一键出片都直接成功（零弹回）", len(shot_ok) == 2, str(results))
        check("有旁白镜自动合成配音后提交",
              any("自动合成旁白" in str(r.get("note", "")) for r in shot_ok), str([r.get("note") for r in shot_ok]))
        check("无旁白镜按静音镜提交",
              any("静音镜" in str(r.get("note", "")) for r in shot_ok), str([r.get("note") for r in shot_ok]))
        proj_mid = project_service.get_project(pid)
        check("旁白已写入分镜 0", proj_mid["scenes"][0].get("voice") is not None)
        vid_jobs = [j for j in res["jobs"] if j.get("type") == "video"]
        check("两镜视频任务均已提交", len(vid_jobs) == 2, str(res["jobs"]))
        for j in res["jobs"]:
            job_manager.delete(j["job_id"])
    finally:
        image_service.run_image_job = orig_img
        video_service.run_video_job = orig_vid
        import os
        try:
            os.unlink(settings.PROJECTS_DIR / f"{pid}.json")
        except OSError:
            pass


async def test_image_reference_guard():
    print("[15] 缺少参考项：暂停生图改询问用户；占位描述拦截；设定图引用强制")
    from app.services import project_service, image_service
    from app.config import settings

    orig_img = image_service.run_image_job
    image_service.run_image_job = fake_image_job
    proj = project_service.create_project("e2e参考项", "16:9")
    pid = proj["id"]
    try:
        scenes = [
            {"title": f"s{i}", "prompt": f"p{i}", "narration": "", "duration": 5, "gen_mode": "i2v",
             "keyframe_prompt": f"完整关键帧描述：中景，角色小墨站在阳光下，暖光，姿态{i}"}
            for i in range(3)
        ]
        patch_llm([
            tool_call("create_scenes", {"force": True, "characters": [{"name": "小墨", "design": "蓝色圆脸小精灵，红色围巾"}], "scenes": scenes}),
            tool_call("generate_scene_image", {"index": 1}),
            tool_call("generate_scene_image", {"index": 0, "prompt": "kf"}),
            tool_call("generate_scene_image", {"index": 0}),
            tool_call("generate_scene_image", {"index": 1}),
            tool_call("generate_scene_image", {"index": 1, "reference_index": 0}),
            '{"action":"finish","summary":"完成"}',
        ])
        res = await run("生成关键帧", context={"page": "/studio", "project_id": pid, "aspect_ratio": "16:9"})
        results = [s["result"] for s in res["steps"] if s.get("type") == "tool" and isinstance(s.get("result"), dict)]
        errs = [str(r.get("error") or "") for r in results if not r.get("ok")]
        check("缺参考项被拦截且指引询问用户", any("缺少参考项" in e and "询问" in e for e in errs), str(errs))
        check("占位描述被拦截", any("过短" in e for e in errs), str(errs))
        check("未带 reference_index 被拦截", any("reference_index" in e for e in errs), str(errs))
        oks = [r for r in results if r.get("ok") and r.get("image")]
        check("设定图与引用图正常生成 2 张", len(oks) == 2, str(len(oks)))
        check("引用图带 reference_used 标记", any(r.get("reference_used") for r in oks), str(oks))
        for j in res["jobs"]:
            job_manager.delete(j["job_id"])
    finally:
        image_service.run_image_job = orig_img
        import os
        try:
            os.unlink(settings.PROJECTS_DIR / f"{pid}.json")
        except OSError:
            pass


async def main():
    orig_voice = voice_service.run_voice_job
    voice_service.run_voice_job = fake_voice_job
    orig_cap = agent_service._capability_snapshot

    async def fake_cap():
        return {
            "tts_models": "fake-tts",
            "voices": "fake-voice",
            "image_models": "fake-image",
            "video_models": "fake-video",
            "llm_models": "fake-llm",
        }

    agent_service._capability_snapshot = fake_cap
    try:
        await test_batch_voice_verbatim()
        await test_resume_skip()
        await test_malformed_recovers()
        await test_cancel()
        await test_embedded_tools()
        await test_raw_newline_json()
        await test_structured_lines()
        await test_script_pipe_annotation()
        await test_force_regenerate()
        await test_empty_and_duplicate_lines()
        await test_image_and_scene_video()
        await test_scene_append_batches()
        await test_plain_text_preamble_recovers()
        await test_stubborn_plain_text_degrades()
        await test_key_order_tolerant()
        await test_multi_embedded_all_executed()
        await test_same_error_circuit_breaker()
        await test_cancel_isolation()
        await test_empty_summary_synthesizes()
        await test_voice_first_guard()
        await test_image_reference_guard()
    finally:
        voice_service.run_voice_job = orig_voice
        agent_service._capability_snapshot = orig_cap
    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        for f in FAIL:
            print("  失败:", f)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
