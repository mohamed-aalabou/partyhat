from typing import Optional, List

from pydantic import BaseModel, Field


class CodeArtifact(BaseModel):
    """
    Lightweight description of a generated code artifact.

    This model is designed to be safe for long-term storage in Letta memory:
    it should contain only metadata (paths, identifiers, summaries), not the
    full source code blob.

    The optional ``code`` field is intended for short-lived use when writing
    files to disk via a storage backend. Callers MUST strip ``code`` before
    persisting artifacts into user memory.
    """

    path: str = Field(
        ...,
        description="Filesystem path or logical key where the code is stored.",
    )
    language: Optional[str] = Field(
        default=None,
        description="Programming language of the artifact (e.g. 'solidity').",
    )
    description: Optional[str] = Field(
        default=None,
        description="Short description or architecture note for this artifact.",
    )
    contract_names: List[str] = Field(
        default_factory=list,
        description="Names of contracts or primary units defined in this artifact.",
    )
    plan_contract_ids: List[str] = Field(
        default_factory=list,
        description="Stable planned contract identifiers linked to this artifact.",
    )
    related_plan_id: Optional[str] = Field(
        default=None,
        description="Identifier or version/hash of the planning artifact this code was generated from.",
    )
    created_at: Optional[str] = Field(
        default=None,
        description="ISO-8601 timestamp when this artifact was created.",
    )
    code: Optional[str] = Field(
        default=None,
        description="Optional raw source code used transiently when saving to storage; not persisted in long-term memory.",
    )


class CodeGenerationRequest(BaseModel):
    goal: str
    related_plan_id: Optional[str] = None
    constraints: List[str] = []


class CodeReviewComment(BaseModel):
    file_path: str
    line: Optional[int] = None
    comment: str
