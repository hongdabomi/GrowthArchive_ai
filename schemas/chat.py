# 홈 채팅 입력, 분석 결과, 작업 진행/완료 응답 형식
from dataclasses import dataclass
from typing import Literal
from datetime import date
import re

from pydantic import Field, model_validator

from schemas.common import ActivityDecision, AnalysisContext, Identifier, StrictModel, Text, TextAnalysis


class FileDecision(StrictModel):
    id: Identifier
    materialType: Text


class HomeActivityDecision(ActivityDecision):
    # 활동 종류는 자료 유형과 별개이며, 판단 근거가 없으면 null이다.
    category: Identifier | None = None

    @model_validator(mode="after")
    def validate_category(self):
        if self.category in {"all", "전체"}:
            raise ValueError("전체는 활동 유형으로 사용할 수 없습니다.")
        if self.match == "unresolved" and self.category is not None:
            raise ValueError("미확정 활동은 유형을 지정할 수 없습니다.")
        return self


class HomeAnalysis(TextAnalysis):
    activity: HomeActivityDecision
    files: list[FileDecision] = Field(max_length=10)


class ActivityFieldUpdate(StrictModel):
    field: Literal["role", "status", "summary", "startDate", "endDate"]
    value: Text | None

    @model_validator(mode="after")
    def validate_value(self):
        if self.field == "status" and self.value not in {None, "진행 중", "완료"}:
            raise ValueError("지원하지 않는 활동 상태입니다.")
        if self.field in {"startDate", "endDate"} and self.value is not None:
            if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])(?:-\d{2})?", self.value):
                raise ValueError("유효한 활동 날짜가 필요합니다.")
            date.fromisoformat(self.value if len(self.value) == 10 else self.value + "-01")
        return self


class ActivityUpdate(StrictModel):
    activityId: Identifier
    activityName: Text
    updates: list[ActivityFieldUpdate] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def unique_fields(self):
        if len({item.field for item in self.updates}) != len(self.updates):
            raise ValueError("수정 항목은 중복될 수 없습니다.")
        return self


class HomeResponse(StrictModel):
    # 일반 대화에는 분석·활동 후보를 요구하지 않는다.
    kind: Literal["general", "analysis", "activity-update"]
    content: Text
    analysis: HomeAnalysis | None
    update: ActivityUpdate | None = None

    @model_validator(mode="after")
    def validate_kind(self):
        if (self.kind == "analysis") != (self.analysis is not None):
            raise ValueError("응답 종류와 분석 결과가 일치하지 않습니다.")
        if (self.kind == "activity-update") != (self.update is not None):
            raise ValueError("응답 종류와 수정 내용이 일치하지 않습니다.")
        return self


class ConversationMessage(StrictModel):
    role: Literal["user", "assistant"]
    content: str


class HomeModelInput(StrictModel):
    message: str
    sourceUrl: str | None = None
    linkContent: str | None = None
    context: AnalysisContext
    files: list[dict]
    activeActivityId: Identifier | None = None
    activeActivityPending: bool = False
    recentMessages: list[ConversationMessage] = Field(default_factory=list, max_length=12)


class LinkRequest(StrictModel):
    url: str = Field(min_length=1, max_length=4096)
    conversationId: str | None = Field(default=None, min_length=1, max_length=200)
    requestId: str | None = Field(default=None, min_length=1, max_length=200)


@dataclass(frozen=True)
class UploadedFile:
    id: str
    name: str
    mime_type: str
    content: bytes
