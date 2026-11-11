#  확정 기록 입력과 성장 유형/판단 결과 형식

from datetime import datetime
from enum import StrEnum
from typing import Literal
from pydantic import Field, model_validator
from schemas.common import StrictModel, Text, Identifier, Change


class GrowthType(StrEnum):
    capability_expansion = "capability_expansion"
    performance_improvement = "performance_improvement"
    experience_accumulation = "experience_accumulation"
    efficiency_improvement = "efficiency_improvement"
    experience_transfer = "experience_transfer"


LABELS = {
    "capability_expansion": ("능력 확장", "🛠️"),
    "performance_improvement": ("성과 향상", "📈"),
    "experience_accumulation": ("경험 축적", "🌱"),
    "efficiency_improvement": ("효율성 향상", "⏱️"),
    "experience_transfer": ("경험 전이", "🔗"),
}


class EvidenceSelection(StrictModel):
    evidenceId: Identifier


class ModelSide(StrictModel):
    text: Text
    evidence: list[EvidenceSelection] = Field(min_length=1, max_length=10)


class ModelComparison(StrictModel):
    before: ModelSide
    after: ModelSide


class GrowthChecks(StrictModel):
    personalPerformance: bool
    meaningfulChange: bool
    criteriaMet: bool
    comparableConditions: bool
    similarQuality: bool
    explicitTransfer: bool


class ModelAssessment(StrictModel):
    growthType: GrowthType | None
    status: Literal["confirmed", "possible", "deferred"]
    activityIds: list[Identifier] = Field(min_length=1, max_length=2)
    title: Text
    axis: str = Field(min_length=1, max_length=40)
    description: Text
    comparison: ModelComparison | None
    evidence: list[EvidenceSelection] = Field(min_length=1, max_length=10)
    missingEvidence: list[Text] = Field(max_length=10)
    checks: GrowthChecks

    @model_validator(mode="after")
    def consistent(self):
        if len(set(self.activityIds)) != len(self.activityIds):
            raise ValueError("활동 ID가 중복됩니다.")
        if self.status == "confirmed" and (self.growthType is None or self.missingEvidence):
            raise ValueError("성장 확인에는 유형과 충분한 근거가 필요합니다.")
        if self.status != "confirmed" and not self.missingEvidence:
            raise ValueError("가능성·보류에는 부족한 조건을 설명해야 합니다.")
        return self

class FileReference(StrictModel):
    id: Identifier
    name: Text


class ActivityMetadata(StrictModel):
    category: Text | None
    role: Text | None
    summary: Text | None
    startDate: Text | None
    endDate: Text | None


class ContextRecord(StrictModel):
    id: Identifier
    activityId: Identifier
    activityName: Text
    message: str
    summary: Text
    changes: list[Change]
    savedAt: Text
    updatedAt: Text
    activity: ActivityMetadata
    files: list[FileReference]


class JourneyContext(StrictModel):
    records: list[ContextRecord] = Field(min_length=1, max_length=100)
    recordCount: int = Field(ge=1, le=100)
    sourceFingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def snapshot(self):
        ids = [r.id for r in self.records]
        if self.recordCount != len(ids) or len(ids) != len(set(ids)):
            raise ValueError("기록 수·ID가 일치하지 않습니다.")
        for record in self.records:
            for stamp in (record.savedAt, record.updatedAt):
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("저장 시각에는 시간대가 필요합니다.")
        if len(self.model_dump_json()) > 250_000:
            raise ValueError("Journey 문맥 한도를 초과했습니다.")
        return self




class ModelSummary(StrictModel):
    title: Text
    description: Text
    evidence: list[EvidenceSelection] = Field(min_length=1, max_length=20)


class ModelBadge(StrictModel):
    label: str = Field(min_length=1, max_length=40)
    icon: str = Field(min_length=1, max_length=16)
    evidence: list[EvidenceSelection] = Field(min_length=1, max_length=5)


class JourneyModel(StrictModel):
    summary: ModelSummary
    assessments: list[ModelAssessment] = Field(max_length=20)
    badges: list[ModelBadge] = Field(max_length=10)
