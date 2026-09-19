"""错误三分类：骨架对失败的处置策略由异常类别决定，不靠解析消息猜。

- RetryableError     自动重试：网络抖动 / provider 超时 / 限流
- FatalError         干净中止：契约违规 / schema 修复失败 / 预算耗尽；保留检查点与已完成产物
- NeedsUserInputError 进确认面板：review 门禁 / 素材归属歧义 / 剧本无法解析；绝不瞎猜
"""


class HarnessError(Exception):
    """harness 层异常基类。"""


class RetryableError(HarnessError):
    """可自动重试，由调用方按退避策略处理。"""


class FatalError(HarnessError):
    """不可自动恢复：中止当前 run，保留已完成产物与检查点。"""


class NeedsUserInputError(HarnessError):
    """需要用户决策才能继续。携带 question 字段供确认面板展示。"""

    def __init__(self, question: str, **context):
        super().__init__(question)
        self.question = question
        self.context = context


class InvariantViolation(FatalError):
    """状态写入口的不变量检查失败：非法状态不可表示，整次写入被拒绝。"""


class VersionConflictError(HarnessError):
    """乐观锁冲突：客户端基线版本落后于服务端（HTTP 409）。"""

    def __init__(self, message: str, current_version: int):
        super().__init__(message)
        self.current_version = current_version


class ProjectNotFoundError(HarnessError):
    """项目不存在或已被归档。"""


class RunBusyError(HarnessError):
    """single-flight 冲突：该项目已有进行中的 run/pipeline。"""

    def __init__(self, message: str, active_run: dict | None = None):
        super().__init__(message)
        self.active_run = active_run
