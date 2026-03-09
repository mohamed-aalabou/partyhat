"""CRUD for users and projects."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.db.models import Project, User


async def create_user(
    session: AsyncSession,
    wallet: str | None = None,
    user_id: uuid.UUID | None = None,
) -> User:
    """Create a user. If user_id is provided, use it; otherwise generate."""
    user = User(
        id=user_id or uuid.uuid4(),
        wallet=wallet,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Fetch user by id."""
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_user_by_wallet(session: AsyncSession, wallet: str) -> User | None:
    """Fetch user by wallet address."""
    result = await session.execute(select(User).where(User.wallet == wallet))
    return result.scalar_one_or_none()


async def create_project(
    session: AsyncSession,
    user_id: uuid.UUID,
    name: str | None = None,
    project_id: uuid.UUID | None = None,
) -> Project:
    """Create a project for the given user."""
    project = Project(
        id=project_id or uuid.uuid4(),
        user_id=user_id,
        name=name,
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def get_project(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> Project | None:
    """Fetch project by id. If user_id is provided, ensure project belongs to user."""
    result = await session.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        return None
    if user_id is not None and project.user_id != user_id:
        return None
    return project


async def list_projects_by_user(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> list[Project]:
    """List all projects for a user."""
    result = await session.execute(
        select(Project).where(Project.user_id == user_id).order_by(Project.created_at.desc())
    )
    return list(result.scalars().all())
