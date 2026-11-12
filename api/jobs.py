# 분석 진행 상태 및 결과 조회

from fastapi import APIRouter, Request
from core.errors import AnalysisError

router = APIRouter(prefix="/api/chat/jobs", tags=["홈", "분석 작업"])


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request):
    if not job_id.startswith("ai-") or len(job_id) > 200:
        raise AnalysisError(404, "job_missing", "분석 작업을 찾을 수 없어요.")
    return await request.app.state.jobs.get(job_id)
