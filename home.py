#홈 챗봇, 활동 연결, 변화 감지

from collections import Counter
import re

from core.errors import AnalysisError
from llm_client import structured_response
from prompts.home import HOME_PROMPT
from documents import validate_files, build_parts
from schemas.chat import HomeResponse, HomeModelInput


KEY_FACT = re.compile(r"^(?:[-*#]\s*)*(?:활동 상태|담당 역할|활동 기간|활동 시작일|활동 종료일)\s*[:：]\s*\S")
NUMBER_CHANGE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:분|시간|초|건|개|회|명|%|점)?\s*(?:→|->|에서|부터)\s*\d")
EXPERIENCE_USE = re.compile(r"(?:경험|지식|노하우|방법|피드백).{0,100}(?:적용|활용|옮겨)")
MEASURED_RESULT = re.compile(r"(?:평균|정확도|회귀 검사|처리 시간|소요 시간|작업 건수).{0,180}\d")
COMPARISON_CONDITION = re.compile(r"(?:같은|동일한).{0,160}\d+.{0,80}(?:측정|평가)")
LEARNED_EXPERIENCE = re.compile(r"(?:처음.{0,100}(?:구현|작성|익혔)|경험.{0,100}쌓)")


def preserve_key_facts(summary, sources):
    # 후속 분석에는 원본 파일이 전달되지 않으므로 빠진 원문 구절만 요약에 남긴다.
    facts = []
    for source in sources:
        for raw in source.splitlines():
            for sentence in re.split(r"(?<=[.!?])\s+", raw):
                fact = sentence.strip().strip("-*# ")
                if (not 4 <= len(fact) <= 300 or fact in summary or fact in facts or
                        not (KEY_FACT.search(fact) or NUMBER_CHANGE.search(fact) or
                             EXPERIENCE_USE.search(fact) or MEASURED_RESULT.search(fact) or
                             COMPARISON_CONDITION.search(fact) or LEARNED_EXPERIENCE.search(fact))):
                    continue
                facts.append(fact)
                if len(facts) == 12:
                    break
            if len(facts) == 12:
                break
        if len(facts) == 12:
            break
    if not facts:
        return summary
    supplement = " 자료에는 " + ", ".join(f"‘{fact}’" for fact in facts) + "라고 명시돼 있어요."
    return summary + supplement if len(summary) + len(supplement) <= 20_000 else summary


def find_source_quote(quote, sources):
    for source in sources:
        if quote in source:
            return quote
    # 줄바꿈·연속 공백만 다른 인용은 원문에 있는 실제 구절로 되돌린다.
    if not 12 <= len(quote) <= 500 or len(quote.split()) < 2:
        return None
    pattern = r"\s+".join(re.escape(part) for part in quote.split())
    for source in sources:
        match = re.search(pattern, source)
        if match:
            return match.group()
    return None


async def analyze_home(*, message, files, source_url, link_content, context, config, active_activity_id=None, active_activity_pending=False, recent_messages=None):
    validate_files(files)
    metadata = [{"id": file.id, "name": file.name} for file in files]
    payload = HomeModelInput(message=message, files=metadata, sourceUrl=source_url,
                             linkContent=link_content, context=context, activeActivityId=active_activity_id,
                             activeActivityPending=active_activity_pending, recentMessages=recent_messages or [])
    parts, text_sources = build_parts(payload, files)
    current_sources = [message, *(file.content.decode("utf-8-sig") for file in files
                                  if file.name.lower().endswith((".txt", ".md")))]
    response = await structured_response(config, instructions=HOME_PROMPT,
                                         input=[{"role": "user", "content": parts}], schema=HomeResponse)
    if response.kind in {"general", "activity-update"}:
        # 첨부 자료를 일반 대화로 무시하지 않고 분석 응답을 요구한다.
        if files or source_url:
            raise AnalysisError(502, "invalid_analysis", "첨부한 자료의 분석 결과를 확인하지 못했어요. 다시 시도해 주세요.")
        if response.kind == "activity-update":
            activities = {activity.id: activity for activity in context.activities}
            if response.update.activityId not in activities:
                raise AnalysisError(502, "invalid_activity_update", "수정할 활동을 확인하지 못했어요. 활동명을 함께 알려 주세요.")
            response.update.activityName = activities[response.update.activityId].name
        return response
    result = response.analysis
    activities = {activity.id: activity for activity in context.activities}
    decision = result.activity
    if decision.match == "existing":
        if decision.id not in activities:
            raise AnalysisError(502, "invalid_analysis", "분석 결과의 활동 ID가 실제 문맥과 일치하지 않아요.")
        decision.name = activities[decision.id].name
        for record in context.records:
            if record.activityId == decision.id:
                text_sources.extend(value for value in [record.message, record.summary] if value)
        decision.category = activities[decision.id].category
    elif decision.id is not None:
        raise AnalysisError(502, "invalid_analysis", "신규 활동에 잘못된 ID가 반환됐어요.")
    if decision.match == "unresolved":
        decision.name, decision.category = "활동 미확정", None
    expected = {file.id for file in files}
    if len(result.files) != len(expected) or {file.id for file in result.files} != expected:
        raise AnalysisError(502, "invalid_analysis", "분석 결과의 파일 목록이 실제 첨부와 다릅니다.")
    for change in result.changes:
        for index, quote in enumerate(change.evidence):
            if quote.startswith("file:") and quote[5:] in expected:
                continue
            actual = find_source_quote(quote, text_sources)
            if actual is None:
                raise AnalysisError(502, "invalid_analysis", "분석 근거가 실제 입력·자료와 일치하지 않아요.")
            change.evidence[index] = actual
    result.summary = preserve_key_facts(result.summary, current_sources)
    return response


def build_reply(result, files, source_url, reply_id):
    if result.kind == "activity-update":
        return {"id": reply_id, "role": "assistant", "kind": "activity-update", "content": "활동 정보를 수정하고 있어요.",
                "analysis": None, "update": result.update.model_dump()}
    if result.kind == "general":
        return {"id": reply_id, "role": "assistant", "kind": "general", "content": result.content, "analysis": None}
    content = result.content
    result = result.analysis
    types = {file.id: file.materialType for file in result.files}
    names = {f"file:{file.id}": file.name for file in files}
    changes = [change.model_dump() for change in result.changes]
    for change in changes:
        change["evidence"] = [names.get(value, value) for value in change["evidence"]]
    kind = ("link-analysis" if source_url else "batch-analysis" if len(files) > 1
            else "file-analysis" if files and changes else "no-change" if files else "text-analysis")
    counts = Counter(types.values())
    reply = {"id": reply_id, "role": "assistant", "kind": kind, "content": content,
             "analysis": {"activity": result.activity.model_dump(), "materialType": result.materialType,
                          "stage": result.stage, "summary": result.summary, "changes": changes,
                          "files": [{"name": file.name, "materialType": types[file.id], "previousVersion": None} for file in files],
                          "groups": [{"materialType": key, "count": value} for key, value in counts.items()],
                          "suggestedSaveMode": "record-change" if changes else "archive"}}
    if source_url:
        reply["sourceUrl"] = source_url
    if files and not changes:
        reply["analysis"]["noChangeMessage"] = "이 자료에서 중요한 변화는 확인되지 않았어요."
    return reply
