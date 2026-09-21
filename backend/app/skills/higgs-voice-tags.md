---
name: higgs-voice-tags
description: 本地 Higgs TTS（模型 ID 含 higgs）配音文本的内联控制标签写法（情感/语速/停顿/拟声）。当项目语音模型是本地 Higgs 且需要为 narration 注入表现力时加载；其他 TTS（MiniMax/小米/OpenAI）没有这些标签，写了会被当作正文念出来，绝不使用。
---

# Higgs TTS 控制标签写法（本地模型专用）

配音文本（narration / 语音页正文）可以直接内嵌 `<|类别:值|>` 形式的控制标签。
标签不发音、不计入朗读时长。仅以下 43 个合法，其他标签会被当作文字念出来。

## 放置规则（决定标签是否生效）

- **句首标签**（情感 / 风格 / 语速音高）：放在句子开头，作用于整个句子
- **行内标签**（拟声 / 停顿）：放在效果发生的确切位置
- **拟声特例**：`<|sfx:xxx|>` 后面紧跟拟声词，不留空格——如 `<|sfx:cough|>咳咳，`
- 标签可叠加：一句开头可以既有情感又有语速，句中再插停顿

## 标签目录

**情感（21，句首）** `<|emotion:值|>`：
affection 关爱 / amusement 欢乐 / anger 愤怒 / arousal 激动 / awe 畏惧 /
bitterness 愤懑 / confusion 困惑 / contemplation 沉思 / contentment 满足 /
determination 坚定 / disgust 厌恶 / elation 欢欣 / enthusiasm 热情 / fear 恐惧 /
helplessness 无助 / longing 渴望 / pride 自豪 / relief 如释重负 / sadness 悲伤 /
shame 羞愧 / surprise 惊讶

**风格（3，句首）** `<|style:值|>`：singing 唱 / shouting 喊 / whispering 耳语

**韵律（句首）** `<|prosody:值|>`：speed_very_slow / speed_slow / speed_fast /
speed_very_fast（语速四档）；pitch_low / pitch_high（音高）；expressive_high /
expressive_low（表现力）。注意 speed_very_slow 最慢约 5 秒，更慢用停顿拼。

**行内**：`<|prosody:pause|>` 短停顿 / `<|prosody:long_pause|>` 长停顿；
`<|sfx:值|>`（后紧跟拟声词）：cough 咳嗽 / laughter 笑 / crying 哭 /
screaming 叫 / burping 打嗝 / humming 哼唱 / sigh 叹气 / sniff 吸鼻子 / sneeze 打喷嚏

## 示例（中文旁白）

```
<|emotion:sadness|>他走了，再也没有回来。
<|style:whispering|>可我一直觉得，他还在某个地方看着我。
<|sfx:sigh|>唉…… <|prosody:pause|> 算了，不说这个了。
```

## 给 Agent 的使用守则

1. **默认不写标签**。系统已把 Emotion/Speed 控件自动转成句首标签；只在用户明确
   要"更有表现力/带情绪/加停顿/拟声"时才手写。
2. **一句最多一两个标签**。堆标签会互相打架，输出劣化。
3. **写入 update_scene 的 narration 时**：标签直接嵌在文本里，前后不需要额外标记。
4. **克隆音色**：本地 Higgs 的音色来自"音色资产"（样本+逐字稿），与标签无关，
   由语音配置决定，不要往 narration 里写音色描述。
5. **长文本**：本地 CPU 合成约 9 秒出 1 秒音频，30 秒旁白约 4-5 分钟，属正常等待，
   不要因慢而重试或换模型。
6. 标签仅对本地 Higgs 有效；切到云端 TTS 前必须把 narration 里的标签清掉。
