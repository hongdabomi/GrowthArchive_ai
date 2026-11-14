# 성장 유형별 판단 조건 검증

import re

from core.errors import AnalysisError
from validation.evidence import restore, check_evidence


QUANTIFIED_CHANGE = re.compile(
    r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(ms|분|초|시간|건|개|회|%)\s*"
    r"(?:에서|부터|→|->)\s*(\d+(?:[.,]\d+)?)\s*\2"
)


def quantified_change(comparison):
    before = {(ref["recordId"], ref["quote"]) for ref in comparison["beforeEvidence"]}
    after = {(ref["recordId"], ref["quote"]) for ref in comparison["afterEvidence"]}
    pairs = {match.groups() for _, quote in before & after
             for match in QUANTIFIED_CHANGE.finditer(quote)}
    return next(iter(pairs)) if len(pairs) == 1 else None


def preserve_comparison_numbers(comparison):
    pair = quantified_change(comparison)
    if pair is None:
        return
    first, unit, last = pair
    for field, value in (("before", first + unit), ("after", last + unit)):
        if value not in comparison[field] and len(comparison[field]) + len(value) + 3 <= 500:
            comparison[field] += f" ({value})"


def validate_assessment(item, activity_id, catalog, records):
    evidence = restore(item.evidence, catalog)
    check_evidence(evidence, records)
    evidence_keys = {(ref["recordId"], ref["quote"]) for ref in evidence}
    by_id = {record["id"]: record for record in records}

    def role_evidence(selected, code):
        refs = restore(selected, catalog)
        if not {(ref["recordId"], ref["quote"]) for ref in refs} <= evidence_keys:
            raise AnalysisError(502, code, "역할별 근거가 판단의 전체 근거에 없어요.")
        check_evidence(refs, records)
        return refs

    comparison = None
    if item.comparison:
        comparison = {
            "before": item.comparison.before,
            "after": item.comparison.after,
            "condition": item.comparison.condition,
            "beforeEvidence": role_evidence(item.comparison.beforeEvidence, "comparison_evidence_invalid"),
            "afterEvidence": role_evidence(item.comparison.afterEvidence, "comparison_evidence_invalid"),
        }
        preserve_comparison_numbers(comparison)

    transfer = None
    if item.transfer:
        source = item.transfer.sourceActivity
        source_refs = role_evidence(item.transfer.sourceEvidence, "transfer_evidence_invalid")
        application_refs = role_evidence(item.transfer.applicationEvidence, "transfer_evidence_invalid")
        if item.transfer.targetActivityId != activity_id or not any(
            by_id[ref["recordId"]]["activityId"] == activity_id for ref in application_refs
        ):
            raise AnalysisError(502, "transfer_evidence_invalid", "실제 적용 활동과 근거 기록이 다릅니다.")
        if source.id is None:
            if not any(source.name in ref["quote"] for ref in source_refs):
                raise AnalysisError(502, "transfer_evidence_invalid", "미등록 출발 활동의 이름이 원문에 없어요.")
        elif source.id == activity_id or not any(
            by_id[ref["recordId"]]["activityId"] == source.id
            and by_id[ref["recordId"]]["activityName"] == source.name for ref in source_refs
        ):
            raise AnalysisError(502, "transfer_evidence_invalid", "출발 활동 ID·이름·근거가 일치하지 않아요.")
        transfer = {"sourceActivity": source.model_dump(), "experience": item.transfer.experience,
                    "targetActivityId": activity_id, "application": item.transfer.application,
                    "sourceEvidence": source_refs, "applicationEvidence": application_refs}

    if item.status == "possible" and not item.checks.meaningfulChange:
        raise AnalysisError(502, "growth_criteria_invalid", "변화가 확인되지 않은 결과를 성장 가능성으로 표시할 수 없어요.")
    if item.status != "confirmed":
        return None

    checks = item.checks
    kind = item.growthType
    valid = checks.personalPerformance and checks.meaningfulChange and checks.criteriaMet
    if kind in {"performance_improvement", "efficiency_improvement"}:
        valid = valid and comparison is not None and checks.comparableConditions
    if kind == "efficiency_improvement":
        valid = valid and checks.similarQuality
    if kind == "experience_transfer":
        valid = valid and transfer is not None and checks.explicitTransfer
    elif transfer is not None:
        valid = False
    if not valid:
        raise AnalysisError(502, "growth_criteria_invalid", "성장 확인의 필수 조건이 충족되지 않았어요.")
    result = {"type": kind, "reason": item.reason, "evidence": evidence}
    if comparison:
        result["comparison"] = comparison
    if transfer:
        result["transfer"] = transfer
    return result
