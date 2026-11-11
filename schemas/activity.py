# 활동 전체 요약/역할/상태/기간 분석 형식
from datetime import date
import re
from typing import Literal
from pydantic import Field, model_validator
from schemas.common import StrictModel, Text, Identifier, Change

class Evidence(StrictModel):
    recordId: Identifier
    quote: Text


class Fact(StrictModel):
    value: Text
    evidence: list[Evidence] = Field(min_length=1, max_length=10)


class StatusFact(StrictModel):
    value: Literal["진행 중", "완료"]
    evidence: list[Evidence] = Field(min_length=1, max_length=10)


class DateFact(Fact):
    @model_validator(mode="after")
    def valid_date(self):
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])(?:-\d{2})?", self.value):
            raise ValueError("활동 날짜는 YYYY-MM 또는 YYYY-MM-DD 형식이어야 합니다.")
        date.fromisoformat(self.value if len(self.value) == 10 else self.value + "-01")
        return self


class Period(StrictModel):
    start: DateFact | None
    end: DateFact | None


class ActivityMetadata(StrictModel):
    summary: Fact
    status: StatusFact | None
    role: Fact | None
    period: Period



class ActivityInput(StrictModel):
    id: Identifier
    name: Text
    category: Identifier | None

class PreviousMetadata(StrictModel):
    summary: Text | None
    status: Literal["진행 중", "완료"] | None
    role: Text | None
    start_date: str | None
    end_date: str | None

class ActivityRecord(StrictModel):
    id: Identifier
    message: str
    summary: Text
    changes: list[Change]
    savedAt: str
    updatedAt: str

class ActivityRequest(StrictModel):
    activity: ActivityInput
    previous: PreviousMetadata
    records: list[ActivityRecord] = Field(min_length=1, max_length=100)
    files: list[tuple[Identifier, Identifier, Text]]
    recordCount: int = Field(ge=1, le=100)
    sourceFingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def snapshot(self):
        ids = [r.id for r in self.records]
        if self.recordCount != len(ids) or len(ids) != len(set(ids)):
            raise ValueError("기록 수·ID를 확인해 주세요.")
        if any(row[0] not in ids for row in self.files):
            raise ValueError("파일 관계가 확정 기록과 다릅니다.")
        if len(self.model_dump_json()) > 250_000:
            raise ValueError("활동 문맥이 너무 큽니다.")
        return self
