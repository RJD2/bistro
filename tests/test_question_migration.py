import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("DB", "postgresql+asyncpg://user:password@localhost/bistro")

from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects.postgresql import insert

from migrations.add_questions import QUESTIONS
from migrations.init_db import upgrade_questions_associo
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
