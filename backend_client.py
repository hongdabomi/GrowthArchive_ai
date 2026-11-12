"""백엔드에서 분석 문맥을 받고 작업·초안을 게시한다."""

from urllib.parse import quote, urlsplit

import httpx
from pydantic import Field, ValidationError, model_validator

from core.errors import AnalysisError
from schemas.chat import ConversationMessage
from schemas.common import AnalysisContext, Identifier, StrictModel


class Selection(StrictModel):
    activitiesTotal: int = Field(ge=0)
    activitiesIncluded: int = Field(ge=0, le=50)
    recordsTotal: int = Field(ge=0)
    recordsIncluded: int = Field(ge=0, le=50)
    truncated: bool


class HomeContext(StrictModel):
    context: AnalysisContext
    selection: Selection
    activeActivityId: Identifier | None
    activeActivityPending: bool
    recentMessages: list[ConversationMessage] = Field(max_length=12)

    @model_validator(mode="after")
    def consistent(self):
        s = self.selection
        if (s.activitiesIncluded != len(self.context.activities) or s.recordsIncluded != len(self.context.records)
                or s.activitiesTotal < s.activitiesIncluded or s.recordsTotal < s.recordsIncluded):
            raise ValueError("문맥 선택 개수가 일치하지 않습니다.")
        if any(len(m.content) > 2000 for m in self.recentMessages):
            raise ValueError("대화 문맥 한도를 초과했습니다.")
        if self.activeActivityId and self.activeActivityId not in {a.id for a in self.context.activities}:
            raise ValueError("현재 활동이 문맥에 없습니다.")
        return self


class BackendClient:
    def __init__(self, settings):
        self.settings = settings

    async def request(self, method, path, **kwargs):
        s = self.settings
        if not s.backend_ready:
            raise AnalysisError(503, "backend_not_configured", "백엔드 내부 주소와 토큰을 설정해 주세요.")
        url = urlsplit(s.backend_internal_base_url)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise AnalysisError(503, "backend_not_configured", "백엔드 내부 주소 설정을 확인해 주세요.")
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
                response = await client.request(method, s.backend_internal_base_url + path,
                    headers={"Authorization": "Bearer " + s.internal_service_token.get_secret_value()}, **kwargs)
            if response.status_code >= 300:
                status = response.status_code if response.status_code in {400, 401, 403, 404, 409, 422} else 503
                raise AnalysisError(status, "backend_request_failed", "백엔드 내부 요청을 완료하지 못했어요.")
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise AnalysisError(503, "backend_unavailable", "백엔드에 연결하지 못했어요.") from None

    async def home_context(self, conversation_id):
        data = await self.request("GET", "/internal/home-context", params={"conversationId": conversation_id})
        try:
            return HomeContext.model_validate(data)
        except ValidationError:
            raise AnalysisError(502, "backend_context_invalid", "백엔드 분석 문맥이 계약과 다릅니다.") from None

    async def publish(self, job):
        body = {k: v for k, v in job.items() if k != "revision"}
        body["expectedRevision"] = job.get("revision", 0)
        data = await self.request("PUT", "/internal/chat-jobs/" + quote(job["jobId"], safe=""), json=body)
        return self.snapshot(data, job)

    async def canonical(self, job_id):
        data = await self.request("GET", "/internal/chat-jobs/" + quote(job_id, safe=""))
        return self.snapshot(data, job_id)

    @staticmethod
    def snapshot(data, expected):
        job = data.get("job") if isinstance(data, dict) else None
        required = {"jobId", "analysisId", "conversationId", "requestId", "requestFingerprint", "kind", "status",
                    "phase", "message", "sourceUrl", "reply", "files", "error", "createdAt", "updatedAt", "revision"}
        if (not isinstance(job, dict) or set(job) != required
                or not all(isinstance(job[k], str) and job[k] for k in
                           ("jobId", "analysisId", "conversationId", "requestId", "requestFingerprint", "kind"))
                or type(job["revision"]) is not int or job["revision"] < 1
                or job["status"] not in {"queued", "running", "completed", "failed"}
                or job["phase"] not in {"preparing", "analyzing", "persisting", "completed"}
                or not isinstance(job["files"], list)
                or not isinstance(job["message"], str)):
            raise AnalysisError(502, "backend_snapshot_invalid", "백엔드 작업 스냅샷이 계약과 다릅니다.")
        if isinstance(expected, str):
            valid = job["jobId"] == expected
        else:
            valid = (job["requestId"] == expected["requestId"]
                     and job["requestFingerprint"] == expected["requestFingerprint"]
                     and (job["jobId"] != expected["jobId"] or
                          (job["analysisId"] == expected["analysisId"]
                           and job["conversationId"] == expected["conversationId"])))
        if not valid:
            raise AnalysisError(502, "backend_snapshot_invalid", "분석 작업 식별자가 일치하지 않습니다.")
        return job
