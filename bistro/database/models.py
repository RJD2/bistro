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
    # Choices shown to players; positions correspond to result identities in options.
    # Empty values keep legacy questions pending until an admin fills them in.
    associo = Column(String, nullable=False, default="", server_default="")


class AutotestSubscription(Base):
    __tablename__ = "autotest_subscriptions"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    # NULL means that the user has turned off daily questions.
    next_send_at = Column(DateTime(timezone=True), nullable=True, index=True)


class AutotestDelivery(Base):
    __tablename__ = "autotest_deliveries"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    question_id = Column(Integer, ForeignKey("questions.id"), primary_key=True)
    sent_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class GroupAutotestSubscription(Base):
    __tablename__ = "group_autotest_subscriptions"

    chat_id = Column(BigInteger, primary_key=True)
    next_send_at = Column(DateTime(timezone=True), nullable=True, index=True)


class GroupAutotestDelivery(Base):
    __tablename__ = "group_autotest_deliveries"

    chat_id = Column(BigInteger, ForeignKey("group_autotest_subscriptions.chat_id"), primary_key=True)
    question_id = Column(Integer, ForeignKey("questions.id"), primary_key=True)
    sent_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


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
