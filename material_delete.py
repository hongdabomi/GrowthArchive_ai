"""남은 원본만 재분석한다. 삭제·최종 저장은 백엔드가 수행한다."""

from pydantic import Field, model_validator
from schemas.common import StrictModel, ActivityContext, Identifier, Text, AnalysisContext
from documents import decoded_files, validate_files
from home import analyze_home, build_reply
from core.errors import AnalysisError


class OriginalFile(StrictModel):
    id: Identifier
    name: Text
    mimeType: str = Field(min_length=1, max_length=200)
    size: int = Field(gt=0, le=10*1024*1024)
    contentBase64: str = Field(min_length=1, max_length=14*1024*1024)


class ExcludeFileRequest(StrictModel):
    fileId: Identifier
    recordReply: str = Field(min_length=1, max_length=1_000_000)
    activity: ActivityContext
    files: list[OriginalFile] = Field(min_length=1, max_length=9)

    @model_validator(mode="after")
    def remaining(self):
        if self.fileId in {f.id for f in self.files}:
            raise ValueError("삭제할 파일은 남은 원본 목록에 포함할 수 없습니다.")
        return self


async def analyze_remaining(payload, config):
    files = decoded_files([f.model_dump() for f in payload.files])
    validate_files(files)
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
