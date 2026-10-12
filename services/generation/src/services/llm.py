"""The one LLM seam of this service: "send a system + user prompt, get back a
validated pydantic object". Workflow steps depend on the StructuredLLM
protocol, not on OpenAI, so tests pass a fake and a provider swap is one
class."""

from typing import Protocol, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from src.services.cost import log_response_usage

T = TypeVar("T", bound=BaseModel)


class StructuredLLM(Protocol):
    async def __call__(self, *, model: str, effort: str, system: str, user: str, output_type: type[T]) -> T: ...


class OpenAIStructured:
    """OpenAI Responses API with Structured Outputs - the response is parsed
    straight into `output_type` (strict JSON schema), never free text."""

    def __init__(self, api_key: str, timeout: float = 170.0):
        # no SDK retries: the workflow's own retry is the budget (timeout x attempts < workflow timeout)
        self.client = AsyncOpenAI(api_key=api_key, timeout=timeout, max_retries=0)

    async def __call__(self, *, model: str, effort: str, system: str, user: str, output_type: type[T]) -> T:
        response = await self.client.responses.parse(
            model=model,
            reasoning={"effort": effort},
            input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            text_format=output_type,
        )
        log_response_usage(model, response.usage)  # billed even when the output doesn't parse
        if response.output_parsed is None:
            raise RuntimeError(f"{model} returned no parsable output (status={response.status})")
        return response.output_parsed
