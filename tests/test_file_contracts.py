"""부분 삭제 재분석과 파일 중복 계약의 로컬 검증."""

import asyncio
from copy import deepcopy
import hashlib
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import SecretStr, ValidationError

from backend_client import BackendClient, DuplicateCheck
from core.config import Settings
from core.errors import AnalysisError
from documents import encoded_files
from jobs import JobService, check_duplicates
from material_delete import ExcludeFileRequest, RemainingAnalysis, analyze_remaining
from schemas.chat import HomeResponse, UploadedFile
from schemas.common import Change
from test_service import FakeBackend, home_result


def request(files, version="exclude-file-result-v2"):
    return ExcludeFileRequest.model_validate({
        "schemaVersion": version, "fileId": "deleted", "recordReply": '{"old":"private"}',
        "activity": {"id": "a1", "name": "활동", "category": None, "summary": "DELETE-SECRET"},
        "files": encoded_files(files)})


def model_result(files, evidence=None):
    data = home_result(files).model_dump()
    data["analysis"]["activity"].update(match="existing", id="a1")
    data["analysis"]["changes"] = ([{"before": "30분", "after": "8분", "category": "효율성",
                                       "evidence": ["file:b"]}] if evidence else [])
    data["changeEvidence"] = evidence or []
    return RemainingAnalysis.model_validate(data)


class FileContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = Settings(openai_api_key=SecretStr("test"), exclude_file_v2_enabled=True,
                                 file_dedup_enabled=True, backend_internal_base_url="http://backend/api",
                                 internal_service_token=SecretStr("test"))

    async def test_remaining_originals_rebuild_changes_and_exclude_deleted_source(self):
        files = [UploadedFile("b", "B.txt", "text/plain", b"before: 30\r\n"),
                 UploadedFile("c", "C.md", "text/markdown", b"after: 8\n")]
        evidence = [{"changeIndex": 0, "before": [{"fileId": "b", "quote": "before: 30"}],
                     "after": [{"fileId": "c", "quote": "after: 8"}]}]
        async def respond(*args, **kwargs):
            self.assertNotIn("private", str(kwargs["input"]))
            self.assertNotIn("deleted", str(kwargs["input"]))
            self.assertNotIn("DELETE-SECRET", str(kwargs["input"]))
            self.assertEqual(kwargs["input"][0]["content"][1]["text"].splitlines()[0], "file:b")
            return model_result(files, evidence)
        with patch("material_delete.structured_response", side_effect=respond):
            output = await analyze_remaining(request(files), self.settings)
        self.assertEqual(output["schemaVersion"], "exclude-file-result-v2")
        self.assertEqual(output["recordReply"], '{"old":"private"}')
        self.assertEqual(output["fileIds"], ["b", "c"])
        self.assertEqual(output["reply"]["analysis"]["changes"][0]["evidence"], ["B.txt", "C.md"])
        self.assertEqual(output["changeEvidence"], evidence)

    async def test_remaining_allows_100_changes_only_in_exclude_contract(self):
        file = UploadedFile("b", "B.txt", "text/plain", b"before: 30\nafter: 8")
        data = home_result([file]).model_dump()
        data["analysis"]["activity"].update(match="existing", id="a1")
        change = {"before": "30", "after": "8", "category": "효율성", "evidence": ["file:b"]}
        item = {"before": [{"fileId": "b", "quote": "before: 30"}],
                "after": [{"fileId": "b", "quote": "after: 8"}]}
        data["analysis"]["changes"] = [dict(change) for _ in range(100)]
        data["changeEvidence"] = [{"changeIndex": i, **item} for i in range(100)]
        result = RemainingAnalysis.model_validate(data)
        with patch("material_delete.structured_response", return_value=result):
            output = await analyze_remaining(request([file]), self.settings)
        self.assertEqual(len(output["reply"]["analysis"]["changes"]), 100)
        self.assertEqual([x["changeIndex"] for x in output["changeEvidence"]], list(range(100)))
        with self.assertRaises(ValidationError):
            HomeResponse.model_validate({key: value for key, value in data.items() if key != "changeEvidence"}
                | {"analysis": data["analysis"] | {"changes": data["analysis"]["changes"][:11]}})
        for field in ("changes", "changeEvidence"):
            too_many = deepcopy(data)
            too_many["analysis"]["changes"].append(dict(change)) if field == "changes" else too_many["changeEvidence"].append({"changeIndex": 100, **item})
            with self.assertRaises(ValidationError):
                RemainingAnalysis.model_validate(too_many)

    async def test_remaining_evidence_allows_20_references_but_not_21(self):
        file = UploadedFile("b", "B.txt", "text/plain", b"before: 30\nafter: 8")
        data = home_result([file]).model_dump()
        data["analysis"]["activity"].update(match="existing", id="a1")
        data["analysis"]["changes"] = [{"before": "30", "after": "8", "category": "효율성", "evidence": ["file:b"]}]
        refs = [{"fileId": "b", "quote": "before: 30"} for _ in range(20)]
        data["changeEvidence"] = [{"changeIndex": 0, "before": refs, "after": [{"fileId": "b", "quote": "after: 8"}]}]
        RemainingAnalysis.model_validate(data)
        data["changeEvidence"][0]["before"].append(refs[0])
        with self.assertRaises(ValidationError):
            RemainingAnalysis.model_validate(data)

    async def test_remaining_empty_and_bad_evidence_or_model_failure(self):
        files = [UploadedFile("b", "B.txt", "text/plain", b"only one state")]
        with patch("material_delete.structured_response", return_value=model_result(files)):
            output = await analyze_remaining(request(files), self.settings)
        self.assertEqual(output["reply"]["analysis"]["changes"], [])
        self.assertEqual(output["changeEvidence"], [])
        bad = model_result(files, [{"changeIndex": 0, "before": [{"fileId": "b", "quote": "missing"}],
                                    "after": [{"fileId": "b", "quote": "only one state"}]}])
        with patch("material_delete.structured_response", return_value=bad):
            with self.assertRaises(AnalysisError) as caught:
                await analyze_remaining(request(files), self.settings)
        self.assertEqual(caught.exception.code, "analysis_failed")

    async def test_remaining_rejects_missing_or_deleted_file_evidence(self):
        files = [UploadedFile("b", "B.txt", "text/plain", b"before: 30\nafter: 8")]
        for evidence in (
            [{"changeIndex": 1, "before": [{"fileId": "b", "quote": "before: 30"}],
              "after": [{"fileId": "b", "quote": "after: 8"}]}],
            [{"changeIndex": 0, "before": [{"fileId": "deleted", "quote": "before: 30"}],
              "after": [{"fileId": "b", "quote": "after: 8"}]}],
        ):
            with patch("material_delete.structured_response", return_value=model_result(files, evidence)):
                with self.assertRaises(AnalysisError) as caught:
                    await analyze_remaining(request(files), self.settings)
            self.assertEqual(caught.exception.code, "analysis_failed")
        with patch("material_delete.structured_response", side_effect=AnalysisError(504, "openai_timeout", "timeout")):
            with self.assertRaises(AnalysisError) as caught:
                await analyze_remaining(request(files), self.settings)
        self.assertEqual(caught.exception.code, "analysis_failed")

    async def test_remaining_unreadable_and_unsupported_version(self):
        txt = UploadedFile("b", "B.txt", "text/plain", b"\xff")
        for files in ([txt], [UploadedFile("b", "B.pdf", "application/pdf", b"broken")]):
            with self.assertRaises(AnalysisError) as caught:
                await analyze_remaining(request(files), self.settings)
            self.assertEqual(caught.exception.code, "source_unreadable")
        with self.assertRaises(AnalysisError) as caught:
            await analyze_remaining(request([UploadedFile("b", "B.txt", "text/plain", b"x")], "old"), self.settings)
        self.assertEqual(caught.exception.code, "unsupported_schema_version")

    async def test_duplicate_request_uses_byte_hash_and_order(self):
        backend = BackendClient(self.settings)
        files = [UploadedFile("b", "renamed.txt", "text/plain", b"same"),
                 UploadedFile("c", "same.txt", "text/plain", b"other")]
        returned = {"schemaVersion": "file-dedup-v1", "activityId": "a1", "files": [
            {"inputIndex": 0, "status": "duplicate", "matches": [{"activityId": "a1", "recordId": "r1", "fileId": "f1"}]},
            {"inputIndex": 1, "status": "new", "matches": []}]}
        async def fake(method, path, **kwargs):
            self.assertEqual((method, path), ("POST", "/internal/file-duplicates/check"))
            self.assertEqual(kwargs["json"]["files"][0]["sha256"], hashlib.sha256(b"same").hexdigest())
            self.assertEqual([x["inputIndex"] for x in kwargs["json"]["files"]], [0, 1])
            return returned
        with patch.object(backend, "request", side_effect=fake):
            self.assertEqual((await backend.check_file_duplicates("a1", [
                {"inputIndex": i, "sha256": hashlib.sha256(f.content).hexdigest(), "size": len(f.content)}
                for i, f in enumerate(files)])).files[0].status, "duplicate")

    async def test_duplicate_same_request_pending_and_lookup_failure(self):
        backend = FakeBackend()
        files = [UploadedFile("f1", "a.txt", "text/plain", b"same"),
                 UploadedFile("f2", "b.txt", "text/plain", b"same")]
        backend.check_file_duplicates = AsyncMock(return_value=DuplicateCheck.model_validate({
            "schemaVersion": "file-dedup-v1", "activityId": None,
            "files": [{"inputIndex": i, "status": "pending", "matches": []} for i in range(2)]}))
        result = await check_duplicates(backend, home_result(files), files)
        self.assertEqual(result["files"][0]["status"], "pending")
        self.assertEqual(result["files"][1]["duplicateOfInputIndex"], 0)
        self.assertTrue(result["saveBlocked"])
        client = BackendClient(self.settings)
        with patch.object(client, "request", side_effect=AnalysisError(404, "backend_request_failed", "missing")):
            with self.assertRaises(AnalysisError) as caught:
                await client.check_file_duplicates(None, [{"inputIndex": 0, "sha256": "a"*64, "size": 1}])
        self.assertEqual(caught.exception.code, "duplicate_lookup_unavailable")
        with patch.object(client, "request", side_effect=AnalysisError(422, "duplicate_lookup_limit", "limit")):
            with self.assertRaises(AnalysisError) as caught:
                await client.check_file_duplicates(None, [{"inputIndex": 0, "sha256": "a"*64, "size": 1}])
        self.assertEqual((caught.exception.status_code, caught.exception.code), (422, "duplicate_lookup_limit"))

    async def test_duplicate_response_rejects_wrong_activity_or_missing_index(self):
        client = BackendClient(self.settings)
        entries = [{"inputIndex": 0, "sha256": "a"*64, "size": 1}]
        for response in (
            {"schemaVersion": "file-dedup-v1", "activityId": "other", "files": [
                {"inputIndex": 0, "status": "new", "matches": []}]},
            {"schemaVersion": "file-dedup-v1", "activityId": "a1", "files": [
                {"inputIndex": 1, "status": "new", "matches": []}]},
            {"schemaVersion": "file-dedup-v1", "activityId": "a1", "files": [
                {"inputIndex": 0, "status": "duplicate", "matches": [
                    {"activityId": "other", "recordId": "r", "fileId": "f"}]}]},
        ):
            with patch.object(client, "request", return_value=response):
                with self.assertRaises(AnalysisError) as caught:
                    await client.check_file_duplicates("a1", entries)
            self.assertEqual(caught.exception.code, "duplicate_lookup_unavailable")

    async def test_no_duplicate_allows_changes_and_default_flag_skips_lookup(self):
        backend = FakeBackend()
        backend.check_file_duplicates = AsyncMock(return_value=DuplicateCheck.model_validate({
            "schemaVersion": "file-dedup-v1", "activityId": None,
            "files": [{"inputIndex": 0, "status": "pending", "matches": []}]}))
        files = [UploadedFile("f1", "a.txt", "text/plain", b"new")]
        result = home_result(files)
        result.analysis.changes = [Change.model_validate({"before": "기존", "after": "새로운 수행",
            "category": "변화", "evidence": ["file:f1"]})]
        for enabled in (True, False):
            settings = Settings(openai_api_key=SecretStr("test"), file_dedup_enabled=enabled,
                                backend_internal_base_url="http://backend/api", internal_service_token=SecretStr("test"))
            service = JobService(backend, settings, asyncio.Semaphore(1))
            job, _ = await service.accept(message="새로운 수행", files=files)
            with patch("jobs.analyze_home", return_value=deepcopy(result)):
                await service.run(job["jobId"])
            reply = (await service.get(job["jobId"]))["result"]["reply"]
            self.assertEqual(len(reply["analysis"]["changes"]), 1)
            self.assertEqual("deduplication" in reply, enabled)
        self.assertEqual(backend.check_file_duplicates.await_count, 1)

    async def test_job_persists_dedup_and_blocks_changes_without_dropping_message(self):
        backend = FakeBackend()
        backend.check_file_duplicates = AsyncMock(return_value=DuplicateCheck.model_validate({
            "schemaVersion": "file-dedup-v1", "activityId": None,
            "files": [{"inputIndex": 0, "status": "pending", "matches": []},
                      {"inputIndex": 1, "status": "pending", "matches": []}]}))
        service = JobService(backend, self.settings, asyncio.Semaphore(1))
        files = [UploadedFile("f1", "a.txt", "text/plain", b"same"),
                 UploadedFile("f2", "b.txt", "text/plain", b"same")]
        job, _ = await service.accept(message="새 텍스트", files=files)
        with patch("jobs.analyze_home", return_value=home_result(files)):
            await service.run(job["jobId"])
        public = await service.get(job["jobId"])
        self.assertEqual(public["status"], "completed")
        self.assertEqual(public["result"]["message"], "새 텍스트")
        self.assertTrue(public["result"]["reply"]["deduplication"]["saveBlocked"])
        self.assertEqual(public["result"]["reply"]["analysis"]["changes"], [])


if __name__ == "__main__":
    unittest.main()
