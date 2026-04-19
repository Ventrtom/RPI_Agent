import json
from dataclasses import asdict, dataclass, field
from typing import Literal


@dataclass
class ReasoningStep:
    type: Literal["diagnose", "gather", "act", "reflect", "finalize"]
    thought: str
    tool_calls: list = field(default_factory=list)
    tool_results: list = field(default_factory=list)
    output: str | None = None


@dataclass
class ReasoningContext:
    user_input: str
    steps: list[ReasoningStep] = field(default_factory=list)
    iteration: int = 0

    def add_step(self, step: ReasoningStep) -> None:
        self.steps.append(step)
        if step.type == "diagnose":
            self.iteration += 1

    def compile_result(self) -> str:
        for step in reversed(self.steps):
            if step.type == "finalize" and step.output:
                return step.output
        # Fallback: last step with any output
        for step in reversed(self.steps):
            if step.output:
                return step.output
        return ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)
