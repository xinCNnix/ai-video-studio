"""项目事件总线：状态变更 → SSE 推送。

线程安全：FastAPI 同步路由跑在 threadpool、pipeline/写回跑在事件循环里，
publish 可能从任意线程调用，用 call_soon_threadsafe 投递到订阅者所在 loop。
队列有界，消费者太慢时丢最旧事件（前端收到 version 跳变可主动 GET 补齐）。
"""
import asyncio
import threading
from collections import defaultdict


class EventBus:
    def __init__(self):
        self._subs: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, project_id: str) -> asyncio.Queue:
        """在当前事件循环上注册订阅，返回有界队列。"""
        q: asyncio.Queue = asyncio.Queue(maxsize=16)
        with self._lock:
            self._subs[project_id].append((asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, project_id: str, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs[project_id] = [(lo, x) for lo, x in self._subs.get(project_id, []) if x is not q]

    def publish(self, project_id: str, event: dict) -> None:
        with self._lock:
            subs = list(self._subs.get(project_id, []))
        for loop, q in subs:
            if loop.is_closed():
                continue

            def _put(q: asyncio.Queue = q) -> None:
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    q.put_nowait(event)
                except Exception:  # noqa: BLE001 - 投递失败不影响写入方
                    pass

            try:
                loop.call_soon_threadsafe(_put)
            except RuntimeError:
                continue


bus = EventBus()
