import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects.postgresql import insert

from migrations.add_questions import QUESTIONS
from migrations.init_db import (
    upgrade_chart_messages_live_updates, upgrade_chart_messages_reply_markup, upgrade_chart_messages_selected_choice,
    upgrade_questions_associo,
)
from bistro.database.models import Base, Question


class QuestionMigrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)

    def test_legacy_database_keeps_questions_and_answers_and_upgrade_is_repeatable(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE questions (id INTEGER PRIMARY KEY, word VARCHAR NOT NULL UNIQUE, "
                "options VARCHAR NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO questions (id, word, options) VALUES (7, 'цезарь', 'салат,император')"
            )
            Base.metadata.create_all(connection)
            connection.exec_driver_sql("INSERT INTO users (id, tg_id) VALUES (3, 123)")
            connection.exec_driver_sql(
                "INSERT INTO answers (user_id, question_id, choice) VALUES (3, 7, 'император')"
            )

            upgrade_questions_associo(connection)
            upgrade_questions_associo(connection)

            self.assertEqual(
                connection.exec_driver_sql("SELECT id, word, options, associo FROM questions").all(),
                [(7, "цезарь", "салат,император", "")],
            )
            self.assertEqual(
                connection.exec_driver_sql("SELECT user_id, question_id, choice FROM answers").all(),
                [(3, 7, "император")],
            )
            connection.exec_driver_sql(
                "INSERT INTO questions (word, options) VALUES ('замок', 'слесарь,рыцарь')"
            )
            self.assertEqual(
                connection.exec_driver_sql("SELECT associo FROM questions WHERE word = 'замок'").scalar_one(),
                "",
            )
            columns = {column["name"]: column for column in inspect(connection).get_columns("questions")}
            self.assertFalse(columns["associo"]["nullable"])

    def test_fresh_database_has_associo_and_accepts_repeat_migration(self):
        with self.engine.begin() as connection:
            Base.metadata.create_all(connection)
            upgrade_questions_associo(connection)
            upgrade_questions_associo(connection)
            connection.exec_driver_sql(
                "INSERT INTO questions (word, options) VALUES ('цезарь', 'салат,Рим')"
            )
            self.assertEqual(connection.exec_driver_sql("SELECT associo FROM questions").scalar_one(), "")

    def test_chart_keyboard_upgrade_preserves_existing_targets_and_is_repeatable(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE chart_messages (id INTEGER PRIMARY KEY, bot_id BIGINT NOT NULL, "
                "question_id INTEGER NOT NULL, chat_id BIGINT NOT NULL, "
                "receiver_user_id BIGINT NOT NULL, message_id BIGINT NOT NULL, "
                "ephemeral_message_id BIGINT NOT NULL, caption VARCHAR, parse_mode VARCHAR, "
                "statistics_key VARCHAR NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO chart_messages VALUES "
                "(1, 123, 7, -100, 42, 0, 9, 'Вы император', 'HTML', 'previous')"
            )
            upgrade_chart_messages_reply_markup(connection)
            upgrade_chart_messages_reply_markup(connection)
            self.assertEqual(connection.exec_driver_sql(
                "SELECT question_id, receiver_user_id, ephemeral_message_id, caption, "
                "statistics_key, reply_markup FROM chart_messages"
            ).one(), (7, 42, 9, "Вы император", "previous", None))
            connection.exec_driver_sql(
                "UPDATE chart_messages SET reply_markup = '{\"inline_keyboard\": []}' WHERE id = 1"
            )
            upgrade_chart_messages_reply_markup(connection)
            self.assertEqual(connection.exec_driver_sql(
                "SELECT reply_markup FROM chart_messages WHERE id = 1"
            ).scalar_one(), '{"inline_keyboard": []}')

    def test_fresh_database_has_chart_keyboard_and_accepts_repeat_upgrade(self):
        with self.engine.begin() as connection:
            Base.metadata.create_all(connection)
            upgrade_chart_messages_reply_markup(connection)
            upgrade_chart_messages_reply_markup(connection)
            columns = {column["name"]: column for column in inspect(connection).get_columns("chart_messages")}
            self.assertTrue(columns["reply_markup"]["nullable"])

    def test_postgres_chart_keyboard_upgrade_handles_concurrent_startup(self):
        connection = MagicMock(dialect=SimpleNamespace(name="postgresql"))
        inspector = MagicMock()
        inspector.get_columns.return_value = [{"name": "id"}]
        with patch("migrations.init_db.inspect", return_value=inspector):
            upgrade_chart_messages_reply_markup(connection)
        connection.exec_driver_sql.assert_called_once_with(
            "ALTER TABLE chart_messages ADD COLUMN IF NOT EXISTS reply_markup VARCHAR"
        )

    def test_highlight_upgrade_preserves_legacy_cards_and_is_repeatable(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE chart_messages (id INTEGER PRIMARY KEY, caption VARCHAR, "
                "statistics_key VARCHAR NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO chart_messages VALUES (1, 'Вы император', 'previous')"
            )
            upgrade_chart_messages_selected_choice(connection)
            upgrade_chart_messages_selected_choice(connection)
            self.assertEqual(connection.exec_driver_sql(
                "SELECT caption, statistics_key, selected_choice FROM chart_messages"
            ).one(), ("Вы император", "previous", None))
            connection.exec_driver_sql(
                "UPDATE chart_messages SET selected_choice = 'император' WHERE id = 1"
            )
            upgrade_chart_messages_selected_choice(connection)
            self.assertEqual(connection.exec_driver_sql(
                "SELECT selected_choice FROM chart_messages WHERE id = 1"
            ).scalar_one(), "император")

    def test_fresh_database_has_nullable_highlight_and_accepts_repeat_upgrade(self):
        with self.engine.begin() as connection:
            Base.metadata.create_all(connection)
            upgrade_chart_messages_selected_choice(connection)
            upgrade_chart_messages_selected_choice(connection)
            columns = {column["name"]: column for column in inspect(connection).get_columns("chart_messages")}
            self.assertTrue(columns["selected_choice"]["nullable"])

    def test_postgres_highlight_upgrade_handles_concurrent_startup(self):
        connection = MagicMock(dialect=SimpleNamespace(name="postgresql"))
        inspector = MagicMock()
        inspector.get_columns.return_value = [{"name": "id"}]
        with patch("migrations.init_db.inspect", return_value=inspector):
            upgrade_chart_messages_selected_choice(connection)
        connection.exec_driver_sql.assert_called_once_with(
            "ALTER TABLE chart_messages ADD COLUMN IF NOT EXISTS selected_choice VARCHAR"
        )

    def test_live_update_upgrade_keeps_legacy_snapshots_static_and_is_repeatable(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE chart_messages (id INTEGER PRIMARY KEY, caption VARCHAR, "
                "statistics_key VARCHAR NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO chart_messages VALUES (1, 'Вы император', 'previous')"
            )

            upgrade_chart_messages_live_updates(connection)
            upgrade_chart_messages_live_updates(connection)

            self.assertEqual(connection.exec_driver_sql(
                "SELECT caption, statistics_key, live_updates FROM chart_messages"
            ).one(), ("Вы император", "previous", False))
            connection.exec_driver_sql("UPDATE chart_messages SET live_updates = TRUE WHERE id = 1")
            upgrade_chart_messages_live_updates(connection)
            self.assertTrue(connection.exec_driver_sql(
                "SELECT live_updates FROM chart_messages WHERE id = 1"
            ).scalar_one())
            connection.exec_driver_sql(
                "INSERT INTO chart_messages (id, statistics_key) VALUES (2, 'new')"
            )
            self.assertFalse(connection.exec_driver_sql(
                "SELECT live_updates FROM chart_messages WHERE id = 2"
            ).scalar_one())

    def test_fresh_database_defaults_to_static_charts_and_accepts_repeat_upgrade(self):
        with self.engine.begin() as connection:
            Base.metadata.create_all(connection)
            upgrade_chart_messages_live_updates(connection)
            upgrade_chart_messages_live_updates(connection)
            column = next(column for column in inspect(connection).get_columns("chart_messages")
                          if column["name"] == "live_updates")
            self.assertFalse(column["nullable"])
            connection.exec_driver_sql(
                "INSERT INTO chart_messages "
                "(bot_id, question_id, chat_id, receiver_user_id, message_id, ephemeral_message_id, statistics_key) "
                "VALUES (123, 7, -100, 42, 0, 9, 'new')"
            )
            self.assertFalse(connection.exec_driver_sql(
                "SELECT live_updates FROM chart_messages"
            ).scalar_one())

    def test_postgres_live_update_upgrade_handles_concurrent_startup(self):
        connection = MagicMock(dialect=SimpleNamespace(name="postgresql"))
        inspector = MagicMock()
        inspector.get_columns.return_value = [{"name": "id"}]
        with patch("migrations.init_db.inspect", return_value=inspector):
            upgrade_chart_messages_live_updates(connection)
        connection.exec_driver_sql.assert_called_once_with(
            "ALTER TABLE chart_messages ADD COLUMN IF NOT EXISTS live_updates BOOLEAN NOT NULL DEFAULT FALSE"
        )

    def test_manually_added_nullable_column_keeps_values_and_normalizes_null(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE questions (id INTEGER PRIMARY KEY, word VARCHAR NOT NULL UNIQUE, "
                "options VARCHAR NOT NULL, associo VARCHAR)"
            )
            connection.exec_driver_sql(
                "INSERT INTO questions (word, options, associo) VALUES "
                "('цезарь', 'салат,Рим', 'повар,император'), ('замок', 'слесарь,рыцарь', NULL)"
            )
            upgrade_questions_associo(connection)
            self.assertEqual(
                connection.exec_driver_sql("SELECT word, associo FROM questions ORDER BY id").all(),
                [("цезарь", "повар,император"), ("замок", "")],
            )

    def test_postgres_upgrade_adds_column_with_concurrent_startup_protection(self):
        connection = MagicMock(dialect=SimpleNamespace(name="postgresql"))
        inspector = MagicMock()
        inspector.get_columns.return_value = [{"name": "id"}, {"name": "word"}, {"name": "options"}]
        with patch("migrations.init_db.inspect", return_value=inspector):
            upgrade_questions_associo(connection)
        connection.exec_driver_sql.assert_called_once_with(
            "ALTER TABLE questions ADD COLUMN IF NOT EXISTS associo VARCHAR NOT NULL DEFAULT ''"
        )

    def test_postgres_upgrade_finishes_manually_added_nullable_column(self):
        connection = MagicMock(dialect=SimpleNamespace(name="postgresql"))
        inspector = MagicMock()
        inspector.get_columns.return_value = [{"name": "associo", "nullable": True, "default": None}]
        with patch("migrations.init_db.inspect", return_value=inspector):
            upgrade_questions_associo(connection)
        self.assertEqual([call.args[0] for call in connection.exec_driver_sql.call_args_list], [
            "UPDATE questions SET associo = '' WHERE associo IS NULL",
            "ALTER TABLE questions ALTER COLUMN associo SET DEFAULT ''",
            "ALTER TABLE questions ALTER COLUMN associo SET NOT NULL",
        ])

    def test_seeds_support_mixed_ready_and_pending_questions_without_overwriting_existing(self):
        with self.engine.begin() as connection:
            Base.metadata.create_all(connection)
            connection.execute(insert(Question).values(QUESTIONS).on_conflict_do_nothing(index_elements=["word"]))
            self.assertEqual(
                connection.exec_driver_sql(
                    "SELECT options, associo FROM questions WHERE word = 'цезарь'"
                ).one(),
                ("повар,император", "салат,Рим"),
            )
            self.assertEqual(connection.exec_driver_sql("SELECT COUNT(*) FROM questions WHERE associo = ''").scalar(), 3)
            connection.exec_driver_sql("UPDATE questions SET associo = 'ключ,крепость' WHERE word = 'замок'")
            connection.execute(insert(Question).values(QUESTIONS).on_conflict_do_nothing(index_elements=["word"]))
            self.assertEqual(connection.exec_driver_sql("SELECT COUNT(*) FROM questions").scalar(), len(QUESTIONS))
            self.assertEqual(
                connection.exec_driver_sql("SELECT associo FROM questions WHERE word = 'замок'").scalar_one(),
                "ключ,крепость",
            )


if __name__ == "__main__":
    unittest.main()
