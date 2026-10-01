"""Provider 抽象基类：LLM / Image / TTS / Video 统一接口。"""
from abc import ABC, abstractmethod


class ProviderError(Exception):
    """第三方模型调用失败。"""


class LLMProvider(ABC):
    @abstractmethod
    async def generate(
        self,
        messages: list[dict],
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str: ...


class ImageProvider(ABC):
    @abstractmethod
    async def generate(
        self,
        prompt: str,
        model: str,
        size: str | None = None,
        n: int = 1,
        references: list[tuple[str, bytes]] | None = None,
    ) -> list[bytes]: ...


class TTSProvider(ABC):
    @abstractmethod
    async def synthesize(
        self,
        text: str,
        model: str,
        voice: str,
        speed: float = 1.0,
        emotion: str = "neutral",
        pitch: int = 0,
        vol: float = 1.0,
    ) -> bytes: ...


class VideoProvider(ABC):
    @abstractmethod
    async def submit(self, params: dict) -> str:
        """提交生成任务，返回 provider_task_id。"""

    @abstractmethod
    async def poll(self, task_id: str) -> tuple[str, str | None, str | None]:
        """轮询任务状态，返回 (status, video_url, error)。
        status ∈ {running, success, failed}。"""
