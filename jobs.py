#성장 5종/3단계 판단

from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from core.errors import AnalysisError
from schemas.chat import JobResponse
from documents import encoded_files, decoded_files
from home import analyze_home, build_reply
from links import fetch_link, validate_url


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JobService:
    def __init__(self, backend, settings, semaphore):
        self.backend, self.settings, self.semaphore = backend, settings, semaphore

    async def accept(self, *, message, files, source_url=None, conversation_id=None, request_id=None):
        if not self.settings.model_ready:
            raise AnalysisError(503, "not_configured", "OPENAI_API_KEY와 OPENAI_MODEL을 설정해 주세요.")
        if not self.settings.backend_ready:
            raise AnalysisError(503, "backend_not_configured", "백엔드 내부 주소와 토큰을 설정해 주세요.")
        if source_url:
            validate_url(source_url)
        originals = encoded_files(files)
        raw = {"message": message, "sourceUrl": source_url, "conversationId": conversation_id,
               "files": [{k: v for k, v in f.items() if k != "id"} for f in originals]}
        fingerprint = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        stamp = now()
        job = {"jobId": "ai-" + str(uuid4()), "analysisId": str(uuid4()),
               "conversationId": conversation_id or str(uuid4()), "requestId": request_id or str(uuid4()),
               "requestFingerprint": fingerprint, "kind": "link" if source_url else "chat",
               "status": "queued", "phase": "preparing", "message": message, "sourceUrl": source_url,
               "reply": None, "files": originals, "error": None, "createdAt": stamp, "updatedAt": stamp}
        own_id = job["jobId"]
        job = await self.backend.publish(job)
        return job, job["jobId"] == own_id and job["status"] in {"queued", "running"}

    async def publish(self, job):
        job["updatedAt"] = now()
        return await self.backend.publish(job)

    async def run(self, job_id):
        job = await self.backend.canonical(job_id)
        if job["status"] not in {"queued", "running"}:
            return
        try:
            async with self.semaphore:
                job.update(status="running", phase="preparing")
                job = await self.publish(job)
                context = await self.backend.home_context(job["conversationId"])
                link = await fetch_link(job["sourceUrl"]) if job["sourceUrl"] else None
                job["phase"] = "analyzing"
                job = await self.publish(job)
                result = await analyze_home(message=job["message"], files=decoded_files(job["files"]),
                    source_url=job["sourceUrl"], link_content=link, context=context.context, config=self.settings,
                    active_activity_id=context.activeActivityId, active_activity_pending=context.activeActivityPending,
                    recent_messages=context.recentMessages)
                job["phase"] = "persisting"
                job = await self.publish(job)
                job.update(status="completed", phase="completed",
                           reply=build_reply(result, decoded_files(job["files"]), job["sourceUrl"], "reply-" + job["analysisId"]))
                await self.publish(job)
        except Exception as error:
            failure = error if isinstance(error, AnalysisError) else AnalysisError(500, "analysis_failed", "분석 처리를 완료하지 못했어요.")
            job.update(status="failed", error={"code": failure.code, "error": failure.message}, updatedAt=now())
            try:
                await self.publish(job)
            except AnalysisError:
                # shortcut: 백엔드 장애 시 실패 게시가 불가능하다. 백엔드가 오래된 실행 작업을 실패로 정리해야 한다.
                pass

    async def get(self, job_id):
        return self.public(await self.backend.canonical(job_id))

    @staticmethod
    def public(job):
        status = "running" if job["status"] == "queued" else job["status"]
        result = {"jobId": job["jobId"], "status": status, "analysisProvider": "openai"}
        if status == "failed":
            result.update(job["error"] or {"code": "analysis_failed", "error": "분석이 완료되지 않았어요."})
        else:
            phases = ["preparing", "analyzing", "persisting"]
            labels = ["입력과 기존 기록을 확인하고 있어요.", "AI가 실제 자료를 분석하고 있어요.", "분석 결과를 보관하고 있어요."]
            completed = 3 if status == "completed" else phases.index(job["phase"])
            steps = [{"label": label, "status": "completed" if i < completed else "working"}
                     for i, label in enumerate(labels) if i <= completed]
            result.update(progress={"title": "분석 결과를 준비했어요." if status == "completed" else labels[completed],
                                    "steps": steps}, pollAfterMs=400)
            if status == "completed":
                if not job["reply"]:
                    raise AnalysisError(502, "draft_missing", "완료 초안을 확인하지 못했어요.")
                result["result"] = {"analysisId": job["analysisId"], "conversationId": job["conversationId"],
                                    "message": job["message"], "reply": job["reply"],
                                    "attachments": [{"id": f["id"], "name": f["name"], "size": f["size"]} for f in job["files"]],
                                    "createdAt": job["createdAt"]}
        JobResponse.model_validate(result)
        return result
