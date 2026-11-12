from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ApprovalMode = Literal["auto", "ask", "full"]
ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]


class ChatOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str | None = Field(default=None, min_length=1, max_length=100)
    model: str | None = Field(default=None, min_length=1, max_length=150)
    thinking_level: ThinkingLevel | None = None
    approval_mode: ApprovalMode | None = None

    @model_validator(mode="after")
    def model_pair(self):
        if (self.provider is None) != (self.model is None):
            raise ValueError("Choose a provider and model together.")
        return self
