from enum import Enum
from typing import Optional, List

from pydantic import BaseModel


class DeploymentStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class DeploymentTarget(BaseModel):
    network: str
    name: str
    description: Optional[str] = None


class DeploymentRecord(BaseModel):
    target: DeploymentTarget
    tx_hash: Optional[str] = None
    status: DeploymentStatus
    notes: Optional[str] = None

