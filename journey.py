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
    model_summary_evidence = restore(model.summary.evidence, catalog)
    check_evidence(model_summary_evidence, context["records"])
    events, axes = build_growth(model.events, catalog, context["records"])
    if events:
        lines = [f'{event["activity"]["name"]}: {event["description"]}' for event in events]
        description = ""
        for line in lines:
            if len(description) + len(line) + 1 > 2000:
                break
            description += (" " if description else "") + line
        if any(item.status != "confirmed" for event in model.events for item in event.growthAssessments):
            note = "일부 변화는 근거가 부족해 확인된 성장으로 표시하지 않았어요."
            if len(description) + len(note) + 1 <= 2000:
                description += " " + note
        evidence = list({(ref["recordId"], ref["quote"]): ref
                         for event in events for item in event["growthAssessments"]
                         for ref in item["evidence"]}.values())[:20]
        summary = {"title": events[0]["title"] if len(events) == 1 else f"확인된 성장 {len(events)}건",
                   "description": description, "evidence": evidence}
    else:
        summary = {"title": "확인된 성장 근거가 부족해요",
                   "description": "제공된 기록만으로는 성장을 확인하지 못했어요. 추가 근거가 필요해요.",
                   "evidence": model_summary_evidence}
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
