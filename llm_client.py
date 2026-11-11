"""레포와 같은 OpenAI Responses 구조화 호출. 자동 재시도 없음."""

from openai import AsyncOpenAI, APIError, APITimeoutError, AuthenticationError, RateLimitError
from pydantic import ValidationError

from core.errors import AnalysisError
from core.limits import MAX_OUTPUT_TOKENS


async def structured_response(config, *, instructions, input, schema):
    if not config.model_ready:
        raise AnalysisError(503, "not_configured", "OPENAI_API_KEY와 OPENAI_MODEL을 설정해 주세요.")
    try:
        async with AsyncOpenAI(api_key=config.openai_api_key.get_secret_value(),
                               timeout=config.openai_timeout_seconds, max_retries=0) as client:
            response = await client.responses.parse(
                model=config.openai_model, instructions=instructions, input=input,
                text_format=schema, reasoning={"effort": "low"},
                max_output_tokens=MAX_OUTPUT_TOKENS, store=False,
            )
    except APITimeoutError:
        raise AnalysisError(504, "openai_timeout", "분석 응답 시간이 초과됐어요.") from None
    except AuthenticationError:
        raise AnalysisError(502, "openai_authentication", "OpenAI 인증 설정을 확인해 주세요.") from None
    except RateLimitError:
        raise AnalysisError(429, "openai_rate_limit", "OpenAI 사용 한도를 확인해 주세요.") from None
    except (APIError, ValidationError, ValueError):
        raise AnalysisError(502, "invalid_analysis", "모델 연결 또는 응답 형식을 확인하지 못했어요.") from None
    if any(getattr(part, "type", None) == "refusal"
           for item in response.output for part in (getattr(item, "content", None) or [])):
        raise AnalysisError(422, "analysis_refused", "이 입력의 분석 결과를 제공할 수 없어요.")
    if response.status != "completed" or not isinstance(response.output_parsed, schema):
        raise AnalysisError(502, "analysis_incomplete", "분석 응답이 완료되지 않았어요.")
    return response.output_parsed
