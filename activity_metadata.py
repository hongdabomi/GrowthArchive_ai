"""백엔드가 제공한 확정 기록 스냅샷만으로 활동 전체 정보를 종합한다."""

import json

from core.errors import AnalysisError
from llm_client import structured_response
from prompts.activity_metadata import ACTIVITY_PROMPT
from schemas.activity import ActivityMetadata
from validation.evidence import check_evidence




async def analyze_activity(payload, config):
    context = payload.model_dump()
    result = await structured_response(config, instructions=ACTIVITY_PROMPT,
                                       input=json.dumps(context, ensure_ascii=False), schema=ActivityMetadata)
    for fact in [result.summary, result.status, result.role, result.period.start, result.period.end]:
        if fact:
            check_evidence(fact.evidence, context["records"])
    start = result.period.start.value if result.period.start else payload.previous.start_date
    end = result.period.end.value if result.period.end else payload.previous.end_date
    if start and end and (start[:7] > end[:7] or len(start) == len(end) == 10 and start > end):
        raise AnalysisError(502, "activity_period_invalid", "활동 시작일·종료일을 확인하지 못했어요.")
    return {"sourceFingerprint": payload.sourceFingerprint, "recordCount": payload.recordCount,
            "model": config.openai_model, "promptVersion": "activity-metadata-analysis",
            "metadata": result.model_dump()}
