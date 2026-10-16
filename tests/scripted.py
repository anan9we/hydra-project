"""A scripted stand-in for the LLM. Tests the agent WIRING (MCP, validator, gate), not model quality."""
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel


def scripted(steps):
    """steps: list of ('tool', name, args) or ('final', dict). One step per model response."""
    def fn(messages, info):
        i = sum(1 for m in messages if isinstance(m, ModelResponse))
        kind, *rest = steps[min(i, len(steps) - 1)]
        if kind == "tool":
            return ModelResponse(parts=[ToolCallPart(tool_name=rest[0], args=rest[1])])
        return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=rest[0])])
    return FunctionModel(fn)
