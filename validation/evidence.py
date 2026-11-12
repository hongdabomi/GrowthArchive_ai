"""기록 원문 후보를 만들고, 모델이 선택한 근거를 원래 인용으로 복원한다."""

import json
import re
from core.errors import AnalysisError


def invalid(message="분석 근거가 실제 기록과 일치하지 않아요."):
    return AnalysisError(502, "evidence_invalid", message)


def check_evidence(evidence, records):
    sources = {r["id"]: r for r in records}
    if not evidence:
        raise invalid("분석 근거가 없어요.")
    for ref in evidence:
        ref = ref.model_dump() if hasattr(ref, "model_dump") else ref
        record = sources.get(ref["recordId"])
        if not record or not ref["quote"] or not any(ref["quote"] in (record.get(k) or "") for k in ("message", "summary")):
            raise invalid()


def prepare_catalog(context):
    catalog, metadata = [], []
    for record in context["records"]:
        metadata.append({k: v for k, v in record.items() if k not in {"message", "summary"}})
        seen = set()
        for source in ("message", "summary"):
            for passage in re.split(r"(?<=[.!?。！？])\s+|\n+", record.get(source) or ""):
                passage = passage.strip()
                while passage:
                    end = min(len(passage), 800)
                    if len(passage) > 800:
                        space = passage.rfind(" ", 400, 800)
                        if space != -1:
                            end = space
                    quote, passage = passage[:end].strip(), passage[end:].strip()
                    if quote and quote not in seen:
                        seen.add(quote)
                        catalog.append({"id": f"evidence-{len(catalog)+1}", "recordId": record["id"],
                                        "source": source, "quote": quote})
                        if len(catalog) > 2000:
                            raise AnalysisError(422, "journey_context_limit", "원문 근거 후보 한도를 초과했어요.")
    if not catalog:
        raise AnalysisError(422, "journey_evidence_missing", "분석할 저장 원문이 없어요.")
    payload = {"records": metadata, "recordCount": context["recordCount"], "evidenceCatalog": catalog}
    if len(json.dumps(payload, ensure_ascii=False)) > 250_000:
        raise AnalysisError(422, "journey_context_limit", "모델 입력 문맥 한도를 초과했어요.")
    return payload


def restore(refs, catalog):
    sources = {item["id"]: item for item in catalog}
    result, seen = [], set()
    for ref in refs:
        key = ref.evidenceId if hasattr(ref, "evidenceId") else ref["evidenceId"]
        if key not in sources:
            raise invalid("존재하지 않는 근거 ID가 반환됐어요.")
        item = sources[key]
        pair = item["recordId"], item["quote"]
        if pair not in seen:
            seen.add(pair)
            result.append({"recordId": pair[0], "quote": pair[1]})
    if not result:
        raise invalid()
    return result
