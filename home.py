#홈 챗봇, 활동 연결, 변화 감지

from collections import Counter

from core.errors import AnalysisError
from llm_client import structured_response
from prompts.home import HOME_PROMPT
from documents import validate_files, build_parts
from schemas.chat import HomeResponse, HomeModelInput


async def analyze_home(*, message, files, source_url, link_content, context, config, active_activity_id=None, active_activity_pending=False, recent_messages=None):
    validate_files(files)
    metadata = [{"id": file.id, "name": file.name} for file in files]
    payload = HomeModelInput(message=message, files=metadata, sourceUrl=source_url,
                             linkContent=link_content, context=context, activeActivityId=active_activity_id,
                             activeActivityPending=active_activity_pending, recentMessages=recent_messages or [])
    parts, text_sources = build_parts(payload, files)
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
        for quote in change.evidence:
            if quote.startswith("file:") and quote[5:] in expected:
                continue
            if not any(quote in source for source in text_sources):
                raise AnalysisError(502, "invalid_analysis", "분석 근거가 실제 입력·자료와 일치하지 않아요.")
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
