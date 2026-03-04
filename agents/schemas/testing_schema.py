from enum import Enum
from typing import Optional, List

from pydantic import BaseModel


class TestStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


class TestPlan(BaseModel):
    name: str
    description: str
    targets: List[str]


class TestResultSummary(BaseModel):
    plan_name: str
    status: TestStatus
    passed: int
    failed: int
    notes: Optional[str] = None

