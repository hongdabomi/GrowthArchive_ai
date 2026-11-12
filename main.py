# FastAPI 실행

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.chat import router as chat_router
from api.jobs import router as jobs_router
from api.analysis import router as analysis_router
from backend_client import BackendClient
from core.config import get_settings
from core.errors import AnalysisError
from jobs import JobService


def create_app(settings=None, *, backend=None):
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.jobs = JobService(backend or BackendClient(settings), settings, asyncio.Semaphore(2))
        yield

    app = FastAPI(title="Growth Archive AI", lifespan=lifespan,
                  description="OpenAI Responses API 기반 홈/활동 종합/고정 5종 성장 분석. 최종 기록 저장은 백엔드가 담당합니다.")
    app.state.settings = settings
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Prefer", "Authorization"])

    @app.exception_handler(AnalysisError)
    async def analysis_error(request: Request, error: AnalysisError):
        return JSONResponse({"error": error.message, "code": error.code}, status_code=error.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return JSONResponse({"error": "입력 형식·필수 필드·한도를 확인해 주세요.", "code": "invalid_input"}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        return JSONResponse({"error": "요청을 처리하지 못했어요.", "code": "internal_error"}, status_code=500)

    @app.get("/api/health", tags=["서버 상태"])
    @app.get("/health", tags=["서버 상태"], include_in_schema=False)
    def health():
        return {"status": "ok"}

    for router in (chat_router, jobs_router, analysis_router):
        app.include_router(router)
    return app


app = create_app()
