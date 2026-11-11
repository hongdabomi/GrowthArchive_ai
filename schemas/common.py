#여러 분석 기능 -> 같이 쓰는 활동/기록/근거 형식
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextInput(StrictModel):
    kind: Literal["text"]
    message: Text


class ActivityContext(StrictModel):
    id: Identifier
    name: Text
    category: Identifier | None = None
    summary: Text | None = None
    role: Text | None = None
    status: Literal["진행 중", "완료"] | None = None
    startDate: Text | None = None
    endDate: Text | None = None


class RecordContext(StrictModel):
    id: Identifier
    activityId: Identifier
    message: Text | None = None
    summary: Text | None = None


class AnalysisContext(StrictModel):
    activities: list[ActivityContext] = Field(default_factory=list, max_length=50)
    records: list[RecordContext] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_references(self):
        ids = [activity.id for activity in self.activities]
        record_ids = [record.id for record in self.records]
        if len(ids) != len(set(ids)) or len(record_ids) != len(set(record_ids)):
            raise ValueError("활동·기록 ID는 중복될 수 없습니다.")
        if any(record.activityId not in ids for record in self.records):
            raise ValueError("기록은 제공된 활동 ID에 연결되어야 합니다.")
        if len(self.model_dump_json()) > 100_000:
            raise ValueError("분석 문맥은 JSON 기준 100000자 이내로 제공해 주세요.")
        return self


class ActivityDecision(StrictModel):
    # new는 아직 DB 생성 전 판단이다. 이번 단계에서는 ID를 생성하지 않는다.
    match: Literal["existing", "new", "unresolved"]
    id: Identifier | None
    name: Text
    confidence: Literal["high", "medium", "low"] | None


class Change(StrictModel):
    before: Text
    after: Text
    category: Text
    evidence: list[Text] = Field(min_length=1, max_length=10)


class TextAnalysis(StrictModel):
    activity: ActivityDecision
    materialType: Text
    stage: Text | None
    summary: Text
    changes: list[Change] = Field(max_length=10)
