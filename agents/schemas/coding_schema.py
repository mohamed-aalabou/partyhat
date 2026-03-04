from typing import Optional, List

from pydantic import BaseModel


class CodeArtifact(BaseModel):
    path: str
    content: str
    language: Optional[str] = None
    description: Optional[str] = None


class CodeGenerationRequest(BaseModel):
    goal: str
    related_plan_id: Optional[str] = None
    constraints: List[str] = []


class CodeReviewComment(BaseModel):
    file_path: str
    line: Optional[int] = None
    comment: str

