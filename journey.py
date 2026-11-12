# AI 성장 분석 실행 및 최종 결과 생성

import json
from pydantic import ValidationError
from core.errors import AnalysisError
from llm_client import structured_response
from prompts.journey import JOURNEY_PROMPT
from schemas.journey import JourneyModel, JourneyProviderResponse, LABELS
from growth import build_growth
from validation.evidence import prepare_catalog, restore, check_evidence

PROMPT_ID = "journey-v6"
SCHEMA_ID = "journey-result-v2"
CRITERIA_ID = "growth-criteria-v1"


async def analyze_journey(payload, config):
    context = payload.model_dump()
    model_input = prepare_catalog(context)
    model = await structured_response(config, instructions=JOURNEY_PROMPT,
                                      input=json.dumps(model_input, ensure_ascii=False), schema=JourneyModel)
    catalog = model_input["evidenceCatalog"]
    summary = {**model.summary.model_dump(), "evidence": restore(model.summary.evidence, catalog)}
    check_evidence(summary["evidence"], context["records"])
    events, axes = build_growth(model.events, catalog, context["records"])
    if not events and model.badges:
        raise AnalysisError(502, "invalid_journey", "성장 이벤트 없이 배지만 반환됐어요.")
    badges = []
    for badge in model.badges:
        evidence = restore(badge.evidence, catalog)
        check_evidence(evidence, context["records"])
        badges.append({"id": f"badge-{len(badges)}", "label": badge.label, "icon": badge.icon, "evidence": evidence})
    response = {"schemaVersion": SCHEMA_ID, "criteriaVersion": CRITERIA_ID,
                "sourceFingerprint": payload.sourceFingerprint, "recordCount": payload.recordCount,
                "model": config.openai_model, "promptVersion": PROMPT_ID,
                "result": {"schemaVersion": SCHEMA_ID, "criteriaVersion": CRITERIA_ID,
                           "source": "api", "summary": summary, "badges": badges,
                           "typeOptions": [{"value": "all", "label": "전체"},
                                           *({"value": key, "label": label} for key, label in LABELS.items())],
                           "axisOptions": [{"value": "all", "label": "전체 분야"}, *axes], "events": events}}
    try:
        JourneyProviderResponse.model_validate(response)
    except ValidationError:
        raise AnalysisError(502, "invalid_journey", "Journey 응답 형식을 확인하지 못했어요.") from None
    return response
