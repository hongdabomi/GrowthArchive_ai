# AI 분석 기능, 데이터 검증 및 서버 연동 테스트

import asyncio
import base64
from copy import deepcopy
import json
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from pypdf import PdfWriter

from core.config import Settings, get_settings
from core.errors import AnalysisError
from backend_client import BackendClient
from llm_client import structured_response
from main import create_app
from documents import encoded_files
from schemas.chat import HomeResponse, UploadedFile
from schemas.activity import ActivityRequest, ActivityMetadata
from schemas.journey import JourneyContext, JourneyModel
from activity_metadata import analyze_activity
from home import analyze_home
from schemas.common import AnalysisContext
from documents import validate_files
from journey import analyze_journey
from links import public_target, validate_url
from material_delete import ExcludeFileRequest, analyze_remaining
from validation.evidence import prepare_catalog


class FakeBackend:
    def __init__(self):
        self.jobs = {}
        self.fail_completed = False
        self.context = {
            "context": {"activities": [], "records": []},
            "selection": {"activitiesTotal": 0, "activitiesIncluded": 0, "recordsTotal": 0,
                          "recordsIncluded": 0, "truncated": False},
            "activeActivityId": None, "activeActivityPending": False, "recentMessages": [],
        }

    async def publish(self, job):
        if self.fail_completed and job["status"] == "completed":
            raise AnalysisError(503, "backend_unavailable", "게시 실패")
        if job.get("revision", 0) == 0:
            for previous in self.jobs.values():
                if previous["requestId"] == job["requestId"]:
                    if previous["requestFingerprint"] != job["requestFingerprint"]:
                        raise AnalysisError(409, "request_conflict", "같은 요청 ID에 다른 입력입니다.")
                    return deepcopy(previous)
        old = self.jobs.get(job["jobId"])
        if old and old["revision"] != job.get("revision", 0):
            raise AnalysisError(409, "conflict", "버전 충돌")
        saved = deepcopy(job)
        saved["revision"] = (old["revision"] if old else 0) + 1
        self.jobs[job["jobId"]] = saved
        return deepcopy(saved)

    async def canonical(self, job_id):
        if job_id not in self.jobs:
            raise AnalysisError(404, "job_missing", "작업이 없습니다.")
        return deepcopy(self.jobs[job_id])

    async def home_context(self, conversation_id):
        from backend_client import HomeContext
        return HomeContext.model_validate(self.context)


def home_result(files=()):
    return HomeResponse.model_validate({
        "kind": "analysis", "content": "내용을 확인해 주세요.", "update": None,
        "analysis": {"activity": {"match": "new", "id": None, "name": "독서 모임",
                                    "confidence": "medium", "category": "동아리"},
                     "materialType": "회의록", "stage": None, "summary": "진행 방식을 논의했어요.",
                     "changes": [], "files": [{"id": f.id, "materialType": "회의록"} for f in files]}})


def valid_pdf():
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(output)
    return output.getvalue()


def journey_context(single=False):
    texts = ["저는 모델 학습을 담당했어요.", "이후 예측 API를 직접 구현하고 테스트했어요."]
    if single:
        texts = ["초기에는 모델 학습을 담당했고 이후 예측 API를 직접 구현했어요."]
    return JourneyContext.model_validate({
        "schemaVersion": "journey-result-v2", "criteriaVersion": "growth-criteria-v1",
        "records": [{"id": f"r{i+1}", "activityId": "a1", "activityName": "모델 프로젝트",
                     "message": "", "summary": text, "changes": [],
                     "savedAt": f"2026-0{i+1}-01T00:00:00.000Z", "updatedAt": "2026-11-01T00:00:00Z",
                     "activity": {"category": "프로젝트", "role": None, "summary": None,
                                  "startDate": None, "endDate": None}, "files": []}
                    for i, text in enumerate(texts)],
        "recordCount": len(texts), "sourceFingerprint": "a"*64})


def growth_result(single=False, status="confirmed"):
    first = {"evidenceId": "evidence-1"}
    second = first if single else {"evidenceId": "evidence-2"}
    return {
        "summary": {"title": "수행 범위가 넓어졌어요", "description": "기록의 근거를 분석했어요", "evidence": [first]},
        "badges": [],
        "events": [{"activityId": "a1", "axis": "개발", "title": "API 구현까지 담당",
                    "description": "모델 학습에서 API 구현까지 직접 수행했어요",
                    "growthAssessments": [{
                        "growthType": "capability_expansion", "status": status,
                        "reason": "예측 API를 직접 구현했어요",
                        "comparison": {"before": "모델 학습", "after": "API 구현", "condition": "같은 프로젝트",
                                       "beforeEvidence": [first], "afterEvidence": [second]},
                        "transfer": None,
                        "evidence": [first] if single else [first, second],
                        "missingEvidence": [] if status == "confirmed" else ["수행 조건을 확인해야 해요"],
                        "checks": {"personalPerformance": True, "meaningfulChange": True, "criteriaMet": True,
                                   "comparableConditions": True, "similarQuality": True, "explicitTransfer": False}
                    }]}],
    }


class PublicApiTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(openai_api_key=SecretStr("test-key"), internal_service_token=SecretStr("internal-test"),
                                 backend_internal_base_url="http://backend/api")
        self.backend = FakeBackend()

    def client(self, settings=None):
        return TestClient(create_app(settings or self.settings, backend=self.backend))

    def test_missing_key_still_starts_and_never_calls_model(self):
        settings = Settings()
        with self.client(settings) as client, patch("llm_client.AsyncOpenAI") as model:
            self.assertEqual(client.get("/health").json(), {"status": "ok"})
            self.assertEqual(client.get("/api/health").json(), {"status": "ok"})
            self.assertEqual(client.get("/api/chat/health").json()["status"], "unavailable")
            self.assertEqual(client.post("/api/chat", data={"message": "경험"}).status_code, 503)
            model.assert_not_called()

    def test_file_only_persists_original_and_public_nulls_without_bytes(self):
        async def analyze(**kwargs):
            return home_result(kwargs["files"])
        with self.client() as client, patch("jobs.analyze_home", side_effect=analyze) as model:
            response = client.post("/api/chat", files={"files": ("회의록.txt", b"hello", "text/plain")},
                                   data={"requestId": "same"})
            self.assertEqual(response.status_code, 202)
            job_id = response.json()["jobId"]
            completed = client.get("/api/chat/jobs/"+job_id).json()
            self.assertEqual(completed["status"], "completed")
            analysis = completed["result"]["reply"]["analysis"]
            self.assertIn("stage", analysis)
            self.assertIsNone(analysis["stage"])
            self.assertIsNone(analysis["files"][0]["previousVersion"])
            self.assertNotIn("contentBase64", json.dumps(completed))
            self.assertEqual(self.backend.jobs[job_id]["files"][0]["contentBase64"], "aGVsbG8=")
            repeat = client.post("/api/chat", files={"files": ("회의록.txt", b"hello", "text/plain")},
                                 data={"requestId": "same"})
            self.assertEqual(repeat.json()["jobId"], job_id)
            self.assertEqual(model.call_count, 1)
            conflict = client.post("/api/chat", data={"message": "different", "requestId": "same"})
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(model.call_count, 1)

    def test_sync_general_and_canonical_reply_after_backend_change(self):
        result = HomeResponse(kind="general", content="안녕하세요.", analysis=None, update=None)
        with self.client() as client, patch("jobs.analyze_home", return_value=result):
            response = client.post("/api/chat", data={"message": "안녕"}, headers={"Prefer": "respond-sync"})
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["result"]["reply"]["kind"], "general")
            self.assertIsNone(data["result"]["reply"]["analysis"])
            saved = self.backend.jobs[data["jobId"]]
            saved["reply"]["content"] = "삭제된 자료는 다시 저장할 수 없어요."
            saved["revision"] += 1
            self.assertEqual(client.get("/api/chat/jobs/"+data["jobId"]).json()["result"]["reply"]["content"],
                             "삭제된 자료는 다시 저장할 수 없어요.")

    def test_failed_publish_never_reports_completed(self):
        self.backend.fail_completed = True
        with self.client() as client, patch("jobs.analyze_home", return_value=home_result()):
            response = client.post("/api/chat", data={"message": "활동 기록"}, headers={"Prefer": "respond-sync"})
            self.assertEqual(response.json()["status"], "failed")
            self.assertNotIn("result", response.json())

    def test_backend_snapshot_survives_ai_server_restart(self):
        result = HomeResponse(kind="general", content="보관된 결과", analysis=None, update=None)
        with self.client() as client, patch("jobs.analyze_home", return_value=result):
            job_id = client.post("/api/chat", data={"message": "안녕"},
                                 headers={"Prefer": "respond-sync"}).json()["jobId"]
        with self.client() as client:
            self.assertEqual(client.get("/api/chat/jobs/" + job_id).json()["result"]["reply"]["content"],
                             "보관된 결과")

    def test_analysis_requires_internal_bearer(self):
        with self.client() as client:
            response = client.post("/api/analyze/journey", json={"secret-user-text": "private"})
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("private", response.text)
            response = client.post("/api/analyze/journey", json={},
                                   headers={"Authorization": "Bearer internal-test"})
            self.assertEqual(response.status_code, 422)

    def test_journey_openapi_response_and_raw_request_limit(self):
        with self.client() as client:
            schema = client.get("/openapi.json").json()
            response_schema = schema["paths"]["/api/analyze/journey"]["post"]["responses"]["200"]
            self.assertEqual(response_schema["content"]["application/json"]["schema"]["$ref"],
                             "#/components/schemas/JourneyProviderResponse")
            body = json.dumps(journey_context().model_dump(), ensure_ascii=False) + " " * 250_001
            with patch("api.analysis.analyze_journey") as model:
                response = client.post("/api/analyze/journey", content=body,
                                       headers={"Authorization": "Bearer internal-test",
                                                "Content-Type": "application/json"})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["code"], "journey_context_limit")
            model.assert_not_called()

    def test_journey_http_keeps_unregistered_source_null(self):
        payload = journey_context(single=True)
        payload.records[0].summary = "개인 프로젝트에서 배운 EC2 설정을 모델 프로젝트에 적용했어요."
        data = growth_result(single=True)
        item = data["events"][0]["growthAssessments"][0]
        item.update(growthType="experience_transfer", comparison=None,
                    transfer={"sourceActivity": {"id": None, "name": "개인 프로젝트"},
                              "experience": "EC2 설정", "targetActivityId": "a1",
                              "application": "모델 프로젝트에 적용",
                              "sourceEvidence": [{"evidenceId": "evidence-1"}],
                              "applicationEvidence": [{"evidenceId": "evidence-1"}]})
        item["checks"]["explicitTransfer"] = True
        with patch("journey.structured_response", return_value=JourneyModel.model_validate(data)):
            provider_response = asyncio.run(analyze_journey(payload, self.settings))
        with self.client() as client, patch("api.analysis.analyze_journey", return_value=provider_response):
            response = client.post("/api/analyze/journey", json=payload.model_dump(),
                                   headers={"Authorization": "Bearer internal-test"})
        self.assertEqual(response.status_code, 200)
        assessment = response.json()["result"]["events"][0]["growthAssessments"][0]
        self.assertIsNone(assessment["transfer"]["sourceActivity"]["id"])
        self.assertNotIn("comparison", assessment)


class AnalysisTests(unittest.IsolatedAsyncioTestCase):
    settings = Settings(openai_api_key=SecretStr("test-key"))

    async def test_home_passes_message_link_text_image_and_document_to_model(self):
        files = [UploadedFile("text-1", "notes.txt", "text/plain", b"Written evidence"),
                 UploadedFile("image-1", "screen.png", "image/png", b"image-bytes"),
                 UploadedFile("pdf-1", "report.pdf", "application/pdf", valid_pdf())]
        data = home_result(files).model_dump()
        data["analysis"]["changes"] = [{"before": "Draft", "after": "Revised",
            "category": "revision", "evidence": ["My own revision", "Linked source", "Written evidence"]}]
        response = HomeResponse.model_validate(data)
        with patch("home.structured_response", return_value=response) as model:
            result = await analyze_home(message="My own revision", files=files,
                source_url="https://example.com/report", link_content="Linked source",
                context=AnalysisContext(), config=self.settings)
        model.assert_awaited_once()
        parts = model.call_args.kwargs["input"][0]["content"]
        payload = json.loads(parts[0]["text"])
        self.assertEqual(payload["message"], "My own revision")
        self.assertEqual(payload["linkContent"], "Linked source")
        self.assertTrue(any("Written evidence" in part.get("text", "") for part in parts))
        image = next(part for part in parts if part["type"] == "input_image")
        document = next(part for part in parts if part["type"] == "input_file")
        self.assertEqual(base64.b64decode(image["image_url"].split(",", 1)[1]), files[1].content)
        self.assertEqual(base64.b64decode(document["file_data"].split(",", 1)[1]), files[2].content)
        self.assertEqual(result.analysis.changes[0].evidence, data["analysis"]["changes"][0]["evidence"])

    async def test_home_preserves_explicit_metadata_and_comparison_facts(self):
        original = ("활동 상태: 완료\n담당 역할: 백엔드 개발\n활동 기간: 2026-01~2026-02\n"
                    "제가 작업 시간을 30분→8분으로 줄였어요.\n"
                    "성장아카이브 자동화 경험을 봉사 활동에 적용했어요.")
        file = UploadedFile("f1", "report.txt", "text/plain", original.encode())
        with patch("home.structured_response", return_value=home_result([file])):
            result = await analyze_home(message="자료를 정리해 주세요.", files=[file],
                source_url=None, link_content=None, context=AnalysisContext(), config=self.settings)
        for fact in original.splitlines():
            self.assertIn(fact, result.analysis.summary)

    async def test_corrupt_pdf_rejected_before_model_call(self):
        for content in (b"not-a-pdf", b"%PDF-1.7\ntruncated"):
            with self.subTest(content=content):
                file = UploadedFile("f1", "broken.pdf", "application/pdf", content)
                with patch("home.structured_response") as model, self.assertRaises(AnalysisError) as caught:
                    await analyze_home(message="확인해 주세요.", files=[file], source_url=None,
                        link_content=None, context=AnalysisContext(), config=self.settings)
                self.assertEqual((caught.exception.status_code, caught.exception.code), (422, "invalid_pdf"))
                model.assert_not_called()

    async def analyze(self, data, single=False):
        with patch("journey.structured_response", return_value=JourneyModel.model_validate(data)):
            return await analyze_journey(journey_context(single), self.settings)

    async def test_journey_contract_and_comparison(self):
        result = await self.analyze(growth_result())
        event = result["result"]["events"][0]
        assessment = event["growthAssessments"][0]
        self.assertEqual(set(result), {"schemaVersion", "criteriaVersion", "sourceFingerprint",
                                       "recordCount", "model", "promptVersion", "result"})
        self.assertEqual(set(event), {"id", "occurredAt", "activity", "axis", "title",
                                      "description", "growthAssessments"})
        self.assertEqual(event["occurredAt"], "2026-02")
        self.assertEqual(assessment["type"], "capability_expansion")
        self.assertEqual(assessment["comparison"]["beforeEvidence"][0]["recordId"], "r1")
        self.assertEqual(result["schemaVersion"], "journey-result-v2")
        self.assertEqual(result["criteriaVersion"], "growth-criteria-v1")
        self.assertEqual(result["promptVersion"], "journey-v6")
        self.assertEqual(result["sourceFingerprint"], "a"*64)
        self.assertEqual(len(result["result"]["typeOptions"]), 6)
        self.assertNotIn("evidenceId", json.dumps(result))
        self.assertNotIn("analysis", result["result"])
        self.assertNotIn("evidenceSources", result["result"])

    async def test_journey_summary_only_describes_public_confirmed_events(self):
        data = growth_result()
        data["summary"]["title"] = "봉사에서 시간을 줄였어요"
        data["summary"]["description"] = "봉사 시간을 줄인 개선을 확인했어요."
        result = await self.analyze(data)
        summary = result["result"]["summary"]
        self.assertNotIn("봉사", summary["title"] + summary["description"])
        self.assertEqual(summary["title"], result["result"]["events"][0]["title"])

    async def test_single_record_comparison_is_preserved(self):
        result = await self.analyze(growth_result(single=True), single=True)
        assessment = result["result"]["events"][0]["growthAssessments"][0]
        self.assertEqual(assessment["comparison"]["beforeEvidence"],
                         assessment["comparison"]["afterEvidence"])

    async def test_unregistered_source_transfer_uses_real_quote(self):
        payload = journey_context(single=True).model_copy(deep=True)
        quote = "개인 프로젝트에서 익힌 EC2 설정을 모델 프로젝트에 적용했어요."
        payload.records[0].summary = quote
        data = growth_result(single=True)
        item = data["events"][0]["growthAssessments"][0]
        item.update(growthType="experience_transfer", comparison=None,
                    transfer={"sourceActivity": {"id": None, "name": "개인 프로젝트"},
                              "experience": "EC2 설정", "targetActivityId": "a1",
                              "application": "모델 프로젝트에 적용",
                              "sourceEvidence": [{"evidenceId": "evidence-1"}],
                              "applicationEvidence": [{"evidenceId": "evidence-1"}]})
        item["checks"]["explicitTransfer"] = True
        with patch("journey.structured_response", return_value=JourneyModel.model_validate(data)):
            result = await analyze_journey(payload, self.settings)
        transfer = result["result"]["events"][0]["growthAssessments"][0]["transfer"]
        self.assertIsNone(transfer["sourceActivity"]["id"])
        self.assertEqual(transfer["sourceEvidence"][0]["quote"], quote)

    async def test_possible_and_deferred_are_not_public_events(self):
        for status in ("possible", "deferred"):
            with self.subTest(status=status):
                result = await self.analyze(growth_result(status=status))
                self.assertEqual(result["result"]["events"], [])
                self.assertEqual(result["result"]["axisOptions"],
                                 [{"value": "all", "label": "전체 분야"}])
                self.assertEqual(len(result["result"]["typeOptions"]), 6)

    async def test_unknown_evidence_and_missing_personal_performance_rejected(self):
        bad = growth_result()
        bad["summary"]["evidence"] = [{"evidenceId": "missing"}]
        with self.assertRaises(AnalysisError):
            await self.analyze(bad)
        bad = growth_result()
        bad["events"][0]["growthAssessments"][0]["checks"]["personalPerformance"] = False
        with self.assertRaises(AnalysisError):
            await self.analyze(bad)

    async def test_nested_evidence_must_be_in_assessment(self):
        bad = growth_result()
        bad["events"][0]["growthAssessments"][0]["evidence"] = [{"evidenceId": "evidence-1"}]
        with self.assertRaises(AnalysisError):
            await self.analyze(bad)

    async def test_efficiency_without_quality_and_transfer_without_link_rejected(self):
        for kind in ("efficiency_improvement", "experience_transfer"):
            data = growth_result()
            item = data["events"][0]["growthAssessments"][0]
            item["growthType"] = kind
            item["checks"]["similarQuality"] = False
            with self.subTest(kind=kind), self.assertRaises(AnalysisError):
                await self.analyze(data)

    async def test_metadata_quotes_and_null_preservation_contract(self):
        payload = ActivityRequest.model_validate({
            "activity": {"id": "a1", "name": "프로젝트", "category": None},
            "previous": {"summary": None, "status": None, "role": "개발", "start_date": None, "end_date": None},
            "records": [{k:v for k,v in journey_context().records[0].model_dump().items()
                         if k in {"id","message","summary","changes","savedAt","updatedAt"}}],
            "files": [], "recordCount": 1, "sourceFingerprint": "a"*64})
        data = {"summary": {"value": "모델 학습을 담당했어요.", "evidence": [
                    {"recordId": "r1", "quote": "저는 모델 학습을 담당했어요."}]},
                "status": None, "role": None, "period": {"start": None, "end": None}}
        with patch("activity_metadata.structured_response", return_value=ActivityMetadata.model_validate(data)):
            result = await analyze_activity(payload, self.settings)
            self.assertIsNone(result["metadata"]["role"])
            self.assertEqual(payload.previous.role, "개발")
        data["summary"]["evidence"][0]["quote"] = "없는 문장"
        with patch("activity_metadata.structured_response", return_value=ActivityMetadata.model_validate(data)):
            with self.assertRaises(AnalysisError):
                await analyze_activity(payload, self.settings)

    async def test_remaining_file_analysis_never_uses_old_reply_or_deleted_bytes(self):
        file = UploadedFile("f2", "remain.txt", "text/plain", b"remaining")
        payload = ExcludeFileRequest.model_validate({"fileId": "f1", "recordReply": '{ "old": "DELETE-SECRET" }',
                  "activity": {"id": "a1", "name": "실제 활동", "category": None}, "files": encoded_files([file])})
        async def analyze(**kwargs):
            self.assertNotIn("DELETE-SECRET", str(kwargs))
            self.assertEqual(kwargs["context"].records, [])
            self.assertEqual([f.id for f in kwargs["files"]], ["f2"])
            return home_result(kwargs["files"])
        with patch("material_delete.analyze_home", side_effect=analyze):
            result = await analyze_remaining(payload, self.settings)
        self.assertEqual(result["recordReply"], payload.recordReply)
        self.assertEqual(result["fileIds"], ["f2"])
        self.assertEqual(result["reply"]["analysis"]["changes"], [])
        self.assertEqual(result["reply"]["analysis"]["activity"]["id"], "a1")

    async def test_sdk_options_and_incomplete_no_retry(self):
        response = SimpleNamespace(status="incomplete", output=[], output_parsed=None)
        connection = MagicMock()
        client = MagicMock()
        client.responses.parse = AsyncMock(return_value=response)
        connection.__aenter__ = AsyncMock(return_value=client)
        connection.__aexit__ = AsyncMock(return_value=False)
        with patch("llm_client.AsyncOpenAI", return_value=connection) as factory:
            with self.assertRaises(AnalysisError):
                await structured_response(self.settings, instructions="instruction", input="input", schema=HomeResponse)
            self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
            self.assertEqual(client.responses.parse.await_count, 1)
            options = client.responses.parse.call_args.kwargs
            self.assertFalse(options["store"])
            self.assertEqual(options["reasoning"], {"effort": "low"})

    async def test_private_link_rejected(self):
        with patch("links.socket.getaddrinfo", return_value=[(2,1,6,'',('127.0.0.1',80))]):
            with self.assertRaises(AnalysisError):
                await public_target("http://localhost/")


class ContractTests(unittest.TestCase):
    def test_backend_snapshot_validates_lookup_id_and_duplicate_request(self):
        snapshot = {"jobId": "ai-original", "analysisId": "analysis-1", "conversationId": "conversation-1",
                    "requestId": "request-1", "requestFingerprint": "fingerprint", "kind": "chat",
                    "status": "queued", "phase": "preparing", "message": "자료", "sourceUrl": None,
                    "reply": None, "files": [], "error": None, "createdAt": "now", "updatedAt": "now", "revision": 1}
        self.assertEqual(BackendClient.snapshot({"job": snapshot}, "ai-original"), snapshot)
        with self.assertRaises(AnalysisError):
            BackendClient.snapshot({"job": snapshot}, "ai-different")
        request = dict(snapshot, jobId="ai-new", analysisId="analysis-2", revision=0)
        self.assertEqual(BackendClient.snapshot({"job": snapshot}, request), snapshot)
        with self.assertRaises(AnalysisError):
            BackendClient.snapshot({"job": dict(snapshot, requestFingerprint="other")}, request)

    def test_files_and_invalid_urls(self):
        for url in ("file:///etc/passwd", "http://u:p@example.com", "http://example.com:1234"):
            with self.assertRaises(AnalysisError):
                validate_url(url)
        for file in (UploadedFile("f", "script.zip", "application/zip", b"x"),
                     UploadedFile("f", "empty.txt", "text/plain", b"")):
            with self.assertRaises(AnalysisError):
                validate_files([file])

    def test_growth_schema_and_snapshot_count(self):
        data = growth_result()
        data["events"][0]["growthAssessments"][0]["growthType"] = "feedback"
        with self.assertRaises(ValidationError):
            JourneyModel.model_validate(data)
        payload = journey_context().model_dump()
        payload["recordCount"] = 1
        with self.assertRaises(ValidationError):
            JourneyContext.model_validate(payload)
        payload = journey_context().model_dump()
        payload["schemaVersion"] = "unsupported"
        with self.assertRaises(ValidationError):
            JourneyContext.model_validate(payload)

    def test_catalog_quotes_remain_exact_and_long_segments_are_bounded(self):
        context = journey_context(single=True).model_dump()
        text = "가"*1700
        context["records"][0]["summary"] = text
        catalog = prepare_catalog(context)["evidenceCatalog"]
        self.assertEqual([len(x["quote"]) for x in catalog], [800,100])
        self.assertTrue(all(x["quote"] in text for x in catalog))

    def test_configuration_uses_only_root_openai_names(self):
        with patch("core.config.dotenv_values", return_value={"AI_API_KEY": "old-key", "AI_MODEL": "old-model"}), \
             patch.dict("os.environ", {}, clear=True):
            settings = get_settings()
            self.assertFalse(settings.model_ready)
            self.assertEqual(settings.openai_model, "gpt-5.4-mini")

if __name__ == "__main__":
    unittest.main()
