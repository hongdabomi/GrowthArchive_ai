# 사용자 입력 접수 및 분석 작업 실행

from uuid import uuid4
from fastapi import APIRouter, BackgroundTasks, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse
from core.errors import AnalysisError
from core.limits import MAX_MESSAGE, MAX_FILE_BYTES, MAX_FILES
from schemas.chat import LinkRequest, UploadedFile
from documents import validate_files

router = APIRouter(prefix="/api/chat", tags=["홈"])


@router.get("/health")
async def health(request: Request):
    s = request.app.state.settings
    ready = s.model_ready and s.backend_ready
    return {"status": "ready" if ready else "unavailable",
            "label": "분석 설정 준비됨" if ready else "OpenAI·백엔드 설정이 필요해요.",
            "analysisProvider": "openai"}


def identifier(value):
    if value is not None and not 1 <= len(value.strip()) <= 200:
        raise AnalysisError(400, "invalid_input", "식별자는 1~200자여야 해요.")
    return value.strip() if value is not None else None


@router.post("")
async def chat(request: Request, background_tasks: BackgroundTasks,
               message: str = Form(""), files: list[UploadFile] = File(
                   default=[], json_schema_extra={"items": {"type": "string", "format": "binary"}}),
               conversationId: str | None = Form(None), requestId: str | None = Form(None),
               prefer: str = Header("")):
    if len(message) > MAX_MESSAGE or (not message.strip() and not files):
        raise AnalysisError(400, "empty_input", "20000자 이내의 글 또는 파일을 보내 주세요.")
    if len(files) > MAX_FILES:
        raise AnalysisError(400, "files_too_large", "파일은 최대 10개까지 보내 주세요.")
    originals = []
    try:
        for file in files:
            originals.append(UploadedFile(str(uuid4()), file.filename or "file",
                             file.content_type or "application/octet-stream", await file.read(MAX_FILE_BYTES + 1)))
    finally:
        for file in files:
            await file.close()
    validate_files(originals)
    service = request.app.state.jobs
    job, fresh = await service.accept(message=message, files=originals,
        conversation_id=identifier(conversationId), request_id=identifier(requestId))
    sync = not files and "respond-sync" in prefer.lower()
    if fresh and sync:
        await service.run(job["jobId"])
        return JSONResponse(await service.get(job["jobId"]))
    if fresh:
        background_tasks.add_task(service.run, job["jobId"])
    return JSONResponse(service.public(job), status_code=202 if job["status"] in {"queued","running"} else 200)


@router.post("/links")
async def link(payload: LinkRequest, request: Request, background_tasks: BackgroundTasks):
    service = request.app.state.jobs
    job, fresh = await service.accept(message=payload.url, files=[], source_url=payload.url,
        conversation_id=identifier(payload.conversationId), request_id=identifier(payload.requestId))
    if fresh:
        background_tasks.add_task(service.run, job["jobId"])
    return JSONResponse(service.public(job), status_code=202 if job["status"] in {"queued","running"} else 200)
