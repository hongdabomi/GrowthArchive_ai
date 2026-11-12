# 활동 종합/성장 판단/삭제 전 재분석

from secrets import compare_digest

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from core.errors import AnalysisError
from schemas.activity import ActivityRequest
from schemas.journey import JourneyContext
from activity_metadata import analyze_activity
from journey import analyze_journey
from material_delete import ExcludeFileRequest, analyze_remaining


bearer = HTTPBearer(auto_error=False, scheme_name="InternalServiceToken")


def require_internal(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    token = request.app.state.settings.internal_service_token
    if token is None:
        raise AnalysisError(503, "internal_not_configured", "내부 통신 토큰을 설정해 주세요.")
    if credentials is None or not compare_digest(credentials.credentials.encode(), token.get_secret_value().encode()):
        raise AnalysisError(401, "unauthorized", "내부 요청 인증이 필요해요.")


router = APIRouter(prefix="/api/analyze", dependencies=[Depends(require_internal)], tags=["백엔드 전용 분석"])


@router.post("/activities/{activity_id}")
async def activity(activity_id: str, payload: ActivityRequest, request: Request):
    if activity_id != payload.activity.id:
        raise AnalysisError(400, "activity_mismatch", "경로와 분석 활동 ID가 다릅니다.")
    return await analyze_activity(payload, request.app.state.settings)


@router.post("/journey")
async def journey(payload: JourneyContext, request: Request):
    return await analyze_journey(payload, request.app.state.settings)


@router.post("/records/{record_id}/exclude-file")
async def remaining(record_id: str, payload: ExcludeFileRequest, request: Request):
    if not record_id.strip() or len(record_id) > 200:
        raise AnalysisError(400, "invalid_input", "유효한 기록 ID가 필요해요.")
    return await analyze_remaining(payload, request.app.state.settings)
