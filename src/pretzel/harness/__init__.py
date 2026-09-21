"""The reusable agent harness every pipeline stage runs on.

Public surface:

    AgentSpec    - what makes one stage different (system prompt + tools + model knobs)
    Tool         - one capability exposed to the model
    RunContext   - injected state every tool handler receives
    run_agent    - the loop itself
"""

from .agent import (
    DEFAULT_MODEL,
    AgentRefused,
    AgentResult,
    AgentSpec,
    HarnessError,
    IterationCapExceeded,
    OutputTruncated,
    Usage,
    run_agent,
)
from .tools import (
    MAX_TOOL_RESULT_CHARS,
    RunContext,
    Tool,
    ToolCall,
    ToolRegistry,
    object_schema,
)

__all__ = [
    "DEFAULT_MODEL",
    "MAX_TOOL_RESULT_CHARS",
    "AgentRefused",
    "AgentResult",
    "AgentSpec",
    "HarnessError",
    "IterationCapExceeded",
    "OutputTruncated",
    "RunContext",
    "Tool",
    "ToolCall",
    "ToolRegistry",
    "Usage",
    "object_schema",
    "run_agent",
]
