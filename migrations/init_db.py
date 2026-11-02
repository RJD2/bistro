import asyncio
from sqlalchemy import inspect

from bistro.database.session import engine
from bistro.database.models import Base


def upgrade_questions_associo(connection):
    """Upgrade existing questions without replacing rows or saved answers"""
    columns = {column["name"]: column for column in inspect(connection).get_columns("questions")}
    if "associo" not in columns:
        # PostgreSQL supports IF NOT EXISTS for concurrent container startups.
        if_not_exists = "IF NOT EXISTS " if connection.dialect.name == "postgresql" else ""
        connection.exec_driver_sql(
            f"ALTER TABLE questions ADD COLUMN {if_not_exists}associo VARCHAR NOT NULL DEFAULT ''"
        )
        return

    # Support installations where the column was previously added manually.
    column = columns["associo"]
    if column["nullable"]:
        connection.exec_driver_sql("UPDATE questions SET associo = '' WHERE associo IS NULL")
    if connection.dialect.name == "postgresql":
        if column["default"] is None:
            connection.exec_driver_sql("ALTER TABLE questions ALTER COLUMN associo SET DEFAULT ''")
        if column["nullable"]:
            connection.exec_driver_sql("ALTER TABLE questions ALTER COLUMN associo SET NOT NULL")


def upgrade_chart_messages_reply_markup(connection):
    """Keep result keyboards available when charts refresh after a restart"""
    columns = {column["name"] for column in inspect(connection).get_columns("chart_messages")}
    if "reply_markup" not in columns:
        if_not_exists = "IF NOT EXISTS " if connection.dialect.name == "postgresql" else ""
        connection.exec_driver_sql(
            f"ALTER TABLE chart_messages ADD COLUMN {if_not_exists}reply_markup VARCHAR"
        )


def upgrade_chart_messages_selected_choice(connection):
    """Preserve each result card's highlighted answer across chart refreshes"""
    columns = {column["name"] for column in inspect(connection).get_columns("chart_messages")}
    if "selected_choice" not in columns:
        if_not_exists = "IF NOT EXISTS " if connection.dialect.name == "postgresql" else ""
        connection.exec_driver_sql(
            f"ALTER TABLE chart_messages ADD COLUMN {if_not_exists}selected_choice VARCHAR"
        )


def upgrade_chart_messages_live_updates(connection):
    """Keep legacy cards static until a group game explicitly registers them"""
    columns = {column["name"] for column in inspect(connection).get_columns("chart_messages")}
    if "live_updates" not in columns:
        if_not_exists = "IF NOT EXISTS " if connection.dialect.name == "postgresql" else ""
        connection.exec_driver_sql(
            f"ALTER TABLE chart_messages ADD COLUMN {if_not_exists}live_updates BOOLEAN NOT NULL DEFAULT FALSE"
        )


async def init():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(upgrade_questions_associo)
        await conn.run_sync(upgrade_chart_messages_reply_markup)
        await conn.run_sync(upgrade_chart_messages_selected_choice)
        await conn.run_sync(upgrade_chart_messages_live_updates)


if __name__ == "__main__":
    asyncio.run(init())
