from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, func, BigInteger, Index, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tg_id = Column(BigInteger, unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    answers = relationship("Answer", back_populates="user")


class Answer(Base):
    __tablename__ = "answers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    question_id = Column(Integer, nullable=False)
    choice = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="answers")


class Question(Base):
    __tablename__ = "questions"

    id = Column(Integer, primary_key=True, index=True)
    word = Column(String, nullable=False, unique=True)
    options = Column(String, nullable=False)


class ChartMessage(Base):
    __tablename__ = "chart_messages"
    __table_args__ = (
        UniqueConstraint(
            "bot_id", "chat_id", "receiver_user_id", "message_id", "ephemeral_message_id",
            name="uq_chart_message_target",
        ),
        Index("ix_chart_messages_question", "bot_id", "question_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_id = Column(BigInteger, nullable=False)
    question_id = Column(Integer, nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    receiver_user_id = Column(BigInteger, nullable=False, default=0)
    message_id = Column(BigInteger, nullable=False, default=0)
    ephemeral_message_id = Column(BigInteger, nullable=False, default=0)
    caption = Column(String, nullable=True)
    parse_mode = Column(String, nullable=True)
    statistics_key = Column(String, nullable=False)
