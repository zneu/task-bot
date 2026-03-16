import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Text, Boolean, Integer, JSON
from sqlalchemy.dialects.postgresql import TIMESTAMP
from database.connection import Base


def utcnow():
    return datetime.now(timezone.utc)


def new_id():
    return str(uuid.uuid4())


class Task(Base):
    __tablename__ = "tasks"
    id = Column(String, primary_key=True, default=new_id)
    title = Column(String, nullable=False)
    status = Column(String, default="not_started")
    priority = Column(String, default="medium")
    project = Column(String, nullable=True)
    due_date = Column(TIMESTAMP(timezone=True), nullable=True)
    committed_today = Column(Boolean, default=False)
    notes = Column(Text, nullable=True)
    notion_id = Column(String, nullable=True)
    avoided_count = Column(Integer, default=0)
    created_at = Column(TIMESTAMP(timezone=True), default=utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=utcnow, onupdate=utcnow)


class CheckIn(Base):
    __tablename__ = "checkins"
    id = Column(String, primary_key=True, default=new_id)
    type = Column(String)
    committed_task_ids = Column(JSON, default=list)
    summary = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), default=utcnow)


class Capture(Base):
    __tablename__ = "captures"
    id = Column(String, primary_key=True, default=new_id)
    raw_text = Column(Text)
    source = Column(String)
    items_created = Column(JSON, default=list)
    processed = Column(Boolean, default=False)
    created_at = Column(TIMESTAMP(timezone=True), default=utcnow)


class Note(Base):
    __tablename__ = "notes"
    id = Column(String, primary_key=True, default=new_id)
    title = Column(String, nullable=False)
    raw_transcript = Column(Text, nullable=False)
    summary = Column(Text, nullable=False)
    tags = Column(JSON, default=list)
    source = Column(String, default="voice")
    notion_id = Column(String, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), default=utcnow)


class Person(Base):
    __tablename__ = "people"
    id = Column(String, primary_key=True, default=new_id)
    name = Column(String, nullable=False)
    context = Column(Text, nullable=True)
    follow_up_action = Column(Text, nullable=True)
    follow_up_date = Column(TIMESTAMP(timezone=True), nullable=True)
    notion_id = Column(String, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), default=utcnow)
