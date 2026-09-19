"""图片生成任务执行服务。"""
from ..jobs import job_manager
from ..providers import registry
from . import file_service, self_check


async def run_image_job(job_id: str, provider: str, model: str, credential_id: str | None,
                        prompt: str, size: str | None, n: int, references: list[str]) -> None:
    jm = job_manager
    jm.update(job_id, status="generating")
    image_provider = registry.get_image(credential_id, provider_hint=provider)

    refs: list[tuple[str, bytes]] = []
    for ref in references:
        path = await file_service.materialize(ref)
        refs.append((path.name, path.read_bytes()))

    images = await image_provider.generate(prompt=prompt, model=model, size=size, n=n, references=refs or None)

    jm.update(job_id, status="downloading")
    outputs = []
    for content in images:
        outputs.append(await file_service.save_output("image", content))
    jm.update(
        job_id,
        status="completed",
        outputs=outputs,
        output=outputs[0] if outputs else None,
        self_check=self_check.check_image(images[0], size),
    )
