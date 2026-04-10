import os
import sys
import json
from typing import List

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.tools import tool

from schemas.audit_schema import AuditIssue, AuditReport
from agents.task_tools import TASK_TOOLS


def _get_memory_manager():
    from agents.memory_manager import MemoryManager
    from agents.context import get_project_context

    project_id, user_id = get_project_context()
    return MemoryManager(user_id=user_id or "default", project_id=project_id)


@tool
def save_audit_issue(issue: AuditIssue) -> dict:
    """
    Save or update an audit issue for this user.
    """
    try:
        mm = _get_memory_manager()
        audit_state = mm.get_agent_state("audit")

        issues: List[dict] = audit_state.get("issues", [])
        issues.append(issue.model_dump())
        audit_state["issues"] = issues
        audit_state["open_issues"] = len(issues)
        mm.set_agent_state("audit", audit_state)

        mm.log_agent_action(
            agent_name="audit",
            action="audit_issue_saved",
            output_produced=issue.model_dump(),
            why="Audit agent saved an issue",
            how="save_audit_issue tool",
        )

        return {"success": True, "issue_id": issue.id}
    except Exception as e:
        return {"error": f"Could not save audit issue: {str(e)}"}


@tool
def finalize_audit_report(report: AuditReport) -> dict:
    """
    Finalize and save an audit report for this user.
    """
    try:
        mm = _get_memory_manager()
        audit_state = mm.get_agent_state("audit")

        reports: List[dict] = audit_state.get("reports", [])
        reports.append(report.model_dump())
        audit_state["reports"] = reports
        mm.set_agent_state("audit", audit_state)

        mm.log_agent_action(
            agent_name="audit",
            action="audit_report_finalized",
            output_produced=report.model_dump(),
            why="Audit agent finalized an audit report",
            how="finalize_audit_report tool",
        )

        return {"success": True, "project_name": report.project_name}
    except Exception as e:
        return {"error": f"Could not finalize audit report: {str(e)}"}


@tool
def get_audit_history() -> dict:
    """
    Retrieve audit issues and reports for this user.
    """
    try:
        mm = _get_memory_manager()
        state = mm.get_agent_state("audit")
        return {
            "issues": state.get("issues", []),
            "reports": state.get("reports", []),
        }
    except Exception as e:
        return {"error": f"Could not retrieve audit history: {str(e)}"}


AUDIT_TOOLS = [
    save_audit_issue,
    finalize_audit_report,
    get_audit_history,
] + TASK_TOOLS
