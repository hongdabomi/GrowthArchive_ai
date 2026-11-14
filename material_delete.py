"""남은 원본만 재분석한다. 삭제·최종 저장은 백엔드가 수행한다."""

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from schemas.common import StrictModel, ActivityContext, Identifier, Text, AnalysisContext, Change
from schemas.chat import HomeResponse, HomeModelInput, HomeAnalysis
from documents import decoded_files, validate_files, build_parts, TEXT_FILES
from home import analyze_home, build_reply
from llm_client import structured_response
from prompts.home import HOME_PROMPT
from core.errors import AnalysisError


class OriginalFile(StrictModel):
    id: Identifier
    name: Text
    mimeType: str = Field(min_length=1, max_length=200)
    size: int = Field(gt=0, le=10*1024*1024)
    contentBase64: str = Field(min_length=1, max_length=14*1024*1024)


class ExcludeFileRequest(StrictModel):
    schemaVersion: str | None = None
    fileId: Identifier
    recordReply: str = Field(min_length=1, max_length=1_000_000)
    activity: ActivityContext
    files: list[OriginalFile] = Field(min_length=1, max_length=9)

    @model_validator(mode="after")
    def remaining(self):
        if self.fileId in {f.id for f in self.files}:
            raise ValueError("삭제할 파일은 남은 원본 목록에 포함할 수 없습니다.")
        return self


class FileQuote(StrictModel):
    fileId: Identifier
    quote: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def nonblank(self):
        if not self.quote.strip():
            raise ValueError("원문 인용은 비어 있을 수 없습니다.")
        return self


class ChangeEvidence(StrictModel):
    changeIndex: int = Field(ge=0)
    before: list[FileQuote] = Field(min_length=1, max_length=20)
    after: list[FileQuote] = Field(min_length=1, max_length=20)


class RemainingHomeAnalysis(HomeAnalysis):
    changes: list[Change] = Field(max_length=100)


class RemainingAnalysis(HomeResponse):
    analysis: RemainingHomeAnalysis | None
    changeEvidence: list[ChangeEvidence] = Field(max_length=100)


def normalized(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


async def analyze_remaining_current(payload, config, files):
    sources = {}
    for file in files:
        if Path(file.name).suffix.lower() not in TEXT_FILES:
            raise AnalysisError(422, "source_unreadable", "남은 원본을 읽을 수 없어요.")
        try:
            sources[file.id] = normalized(file.content.decode("utf-8-sig"))
        except UnicodeDecodeError:
            raise AnalysisError(422, "source_unreadable", "남은 원본을 읽을 수 없어요.") from None
    activity = ActivityContext(id=payload.activity.id, name=payload.activity.name, category=payload.activity.category)
    model_input = HomeModelInput(message="남은 원본만 분석", files=[{"id": f.id, "name": f.name} for f in files],
        context=AnalysisContext(activities=[activity], records=[]), activeActivityId=activity.id)
    try:
        parts, _ = build_parts(model_input, files)
        result = await structured_response(config, instructions=HOME_PROMPT + """

# 부분 파일 삭제 후 재분석
남은 원본만으로 이번 기록의 요약과 실제 전후 변화를 새로 분석한다. 기존 결과나 삭제 파일은 입력에 없다.
kind=analysis, update=null로 반환한다. 각 changes 항목마다 동일한 changeIndex의 changeEvidence를 정확히 하나 반환한다.
before와 after에는 각각 해당 주장을 뒷받침하는 남은 파일 ID와 원문 연속 인용을 1개 이상 넣는다.
원문의 줄바꿈만 정규화할 수 있다. 원문에 비교 근거가 없으면 changes=[]와 changeEvidence=[]를 반환한다.
""", input=[{"role": "user", "content": parts}], schema=RemainingAnalysis)
    except AnalysisError as error:
        if error.code in {"file_text_too_large", "file_encoding", "invalid_pdf", "invalid_file"}:
            raise AnalysisError(422, "source_unreadable", "남은 원본을 읽을 수 없어요.") from None
        raise AnalysisError(502, "analysis_failed", "남은 원본 분석을 완료하지 못했어요.") from None
    if result.kind != "analysis" or result.analysis.activity.match != "existing" or result.analysis.activity.id != payload.activity.id:
        raise AnalysisError(502, "analysis_failed", "남은 원본 분석 결과가 올바르지 않아요.")
    if ([item.changeIndex for item in result.changeEvidence] != list(range(len(result.analysis.changes)))
            or len(result.analysis.files) != len(files)
            or [item.id for item in result.analysis.files] != [file.id for file in files]):
        raise AnalysisError(502, "analysis_failed", "변화와 원본 근거의 연결이 올바르지 않아요.")
    for item in result.changeEvidence:
        for ref in [*item.before, *item.after]:
            if ref.fileId not in sources or normalized(ref.quote) not in sources[ref.fileId]:
                raise AnalysisError(502, "analysis_failed", "원문 인용이 남은 파일과 일치하지 않아요.")
        result.analysis.changes[item.changeIndex].evidence = list(dict.fromkeys(
            "file:" + ref.fileId for ref in [*item.before, *item.after]))
    result.analysis.activity.name = payload.activity.name
    result.analysis.activity.category = payload.activity.category
    return {"schemaVersion": "exclude-file-result-v2", "reply": build_reply(result, files, None, "reply-reanalysis"),
            "recordReply": payload.recordReply, "fileIds": [f.id for f in files],
            "changeEvidence": [item.model_dump() for item in result.changeEvidence]}


async def analyze_remaining(payload, config):
    if config.exclude_file_v2_enabled != (payload.schemaVersion == "exclude-file-result-v2") or payload.schemaVersion not in {None, "exclude-file-result-v2"}:
        raise AnalysisError(422, "unsupported_schema_version", "지원하지 않는 재분석 계약 버전이에요.")
    try:
        files = decoded_files([f.model_dump(exclude={"schemaVersion"}) for f in payload.files])
        validate_files(files)
    except AnalysisError as error:
        if config.exclude_file_v2_enabled:
            raise AnalysisError(422, "source_unreadable", "남은 원본을 읽을 수 없어요.") from None
        raise
    if config.exclude_file_v2_enabled:
        return await analyze_remaining_current(payload, config, files)
    result = await analyze_home(
        message="첨부된 남은 파일만으로 기록을 새로 요약해 주세요. 이전 자료와의 변화는 추정하지 마세요.",
        files=files, source_url=None, link_content=None,
        context=AnalysisContext(activities=[payload.activity], records=[]), config=config)
    if result.kind != "analysis":
        raise AnalysisError(502, "invalid_analysis", "남은 원본 분석 결과를 확인하지 못했어요.")
    result.analysis.changes = []
    result.analysis.activity.match = "existing"
    result.analysis.activity.id = payload.activity.id
    result.analysis.activity.name = payload.activity.name
    result.analysis.activity.category = payload.activity.category
    return {"reply": build_reply(result, files, None, "reply-reanalysis"),
            "recordReply": payload.recordReply, "fileIds": [f.id for f in files]}
