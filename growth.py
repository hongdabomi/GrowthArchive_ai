# 검증된 성장 판단을 Journey 이벤트로 변환

from datetime import datetime, timezone
import hashlib
import unicodedata

from core.errors import AnalysisError
from validation.growth import validate_assessment, quantified_change


def build_growth(model_events, catalog, records):
    events, axes = [], {}
    by_id = {record["id"]: record for record in records}
    by_activity = {record["activityId"]: record["activityName"] for record in records}

    for model_event in model_events:
        if model_event.activityId not in by_activity:
            raise AnalysisError(502, "growth_activity_invalid", "성장 이벤트의 활동 ID가 실제 기록에 없어요.")
        assessments = []
        seen_types = set()
        for item in model_event.growthAssessments:
            if item.growthType is not None:
                if item.growthType in seen_types:
                    raise AnalysisError(502, "growth_type_duplicate", "한 이벤트에 같은 성장 유형이 중복됐어요.")
                seen_types.add(item.growthType)
            assessment = validate_assessment(item, model_event.activityId, catalog, records)
            if assessment is None:
                continue
            assessments.append(assessment)
        if not assessments:
            continue

        description = model_event.description
        for assessment in assessments:
            comparison = assessment.get("comparison")
            if assessment["type"] not in {"performance_improvement", "efficiency_improvement"} or comparison is None:
                continue
            pair = quantified_change(comparison)
            if pair is not None:
                first, unit, last = pair
                change = f"{first}{unit} → {last}{unit}"
                if change not in description and len(description) + len(change) + 2 <= 1000:
                    description += f" {change}."

        evidence = {(ref["recordId"], ref["quote"])
                    for assessment in assessments for ref in assessment["evidence"]}
        if not any(by_id[record_id]["activityId"] == model_event.activityId for record_id, _ in evidence):
            raise AnalysisError(502, "growth_activity_invalid", "이벤트 대상 활동의 근거 기록이 없어요.")
        label = " ".join(unicodedata.normalize("NFKC", model_event.axis).split())
        if not label or len(label) > 40 or label.casefold() in {"all", "전체", "전체 분야", "전체분야"}:
            raise AnalysisError(502, "journey_axis_invalid", "성장 분야 이름을 확인하지 못했어요.")
        axis = "axis-" + hashlib.sha256(label.casefold().encode()).hexdigest()[:12]
        axes.setdefault(axis, {"value": axis, "label": label})
        if len(axes) > 8:
            raise AnalysisError(502, "journey_axis_limit", "성장 분야 한도를 초과했어요.")
        latest = max(datetime.fromisoformat(by_id[record_id]["savedAt"].replace("Z", "+00:00"))
                     for record_id, _ in evidence).astimezone(timezone.utc)
        events.append({"id": f"event-{len(events)}", "occurredAt": latest.strftime("%Y-%m"),
                       "activity": {"id": model_event.activityId, "name": by_activity[model_event.activityId]},
                       "axis": axis, "title": model_event.title, "description": description,
                       "growthAssessments": assessments})
    return events, list(axes.values())
