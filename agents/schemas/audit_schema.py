from enum import Enum
from typing import Optional, List

from pydantic import BaseModel


class IssueSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IssueStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"


class AuditIssue(BaseModel):
    id: str
    title: str
    severity: IssueSeverity
    status: IssueStatus = IssueStatus.OPEN
    description: str
    recommendation: Optional[str] = None


class AuditReport(BaseModel):
    project_name: str
    issues: List[AuditIssue]
    summary: Optional[str] = None

