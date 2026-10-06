"""Chat service: coordinate conversation history, provider calls and persistence.

This is the application-layer service for the interactive chat. It depends only
on ports and domain models, never on SQLite or a specific provider adapter.
"""

from __future__ import annotations

import uuid
from typing import Optional, Sequence

from ..domain.models import ChatMessage, ChatSession, HandoverRecord, utc_now
from ..ports.capabilities import ChatPort, ChatUsage
from ..ports.repositories import ChatRepository, HandoverRepository


class ChatService:
    """Coordinate interactive chat conversations."""

    def __init__(
        self,
        chat_port: ChatPort,
        chat_repo: ChatRepository,
        handover_repo: HandoverRepository,
    ) -> None:
        self._chat_port = chat_port
        self._chat_repo = chat_repo
        self._handover_repo = handover_repo

    def start_session(
        self,
        project: str,
        agent_slot: str,
        provider: str,
        model: str,
    ) -> ChatSession:
        """Create and persist a new chat session."""
        session = ChatSession(
            id=str(uuid.uuid4()),
            project=project,
            agent_slot=agent_slot,
            provider=provider,
            model=model,
            created_at=utc_now(),
        )
        self._chat_repo.upsert_session(session)
        return session

    def get_history(self, session_id: str) -> tuple[ChatSession, tuple[ChatMessage, ...]]:
        """Retrieve a session and its full message history."""
        session = self._chat_repo.get_session(session_id)
        if not session:
            raise ValueError(f"Chat session not found: {session_id}")
        messages = self._chat_repo.list_messages(session_id)
        return session, messages

    def ask(self, session_id: str, question: str) -> tuple[ChatMessage, ChatUsage]:
        """Send a question, persist both the question and the answer."""
        session, history = self.get_history(session_id)
        
        # Persist user question
        user_msg = ChatMessage(
            id=str(uuid.uuid4()), role="user", text=question, created_at=utc_now()
        )
        self._chat_repo.append_message(session_id, user_msg)
        
        # Call provider (with history)
        try:
            answer_msg, usage = self._chat_port.ask(session_id, history, question)
            # Assign an ID to the assistant message
            answer = ChatMessage(
                id=str(uuid.uuid4()),
                role=answer_msg.role,
                text=answer_msg.text,
                created_at=answer_msg.created_at,
            )
            # Persist assistant answer
            self._chat_repo.append_message(session_id, answer)
            return answer, usage
        except Exception:
            # If it failed, delete the user message so it doesn't pollute history
            self._chat_repo.delete_message(session_id, user_msg.id)
            raise

    def handover(
        self, session_id: str, message_id: str, actor: str, reason: str
    ) -> HandoverRecord:
        """Hand over a specific assistant message to the lead agent."""
        session, history = self.get_history(session_id)
        
        message = None
        for m in history:
            if m.id == message_id:
                message = m
                break
        
        if not message:
            raise ValueError(f"Message not found: {message_id}")
        
        if message.role != "assistant":
            raise ValueError("Only assistant messages can be handed over.")
        
        record = HandoverRecord(
            id=str(uuid.uuid4()),
            project=session.project,
            session_id=session_id,
            message_id=message_id,
            text=message.text,
            provider=session.provider,
            model=session.model,
            actor=actor,
            reason=reason,
            created_at=utc_now(),
        )
        self._handover_repo.upsert(record)
        return record

    def list_handovers(self, project: str) -> tuple[HandoverRecord, ...]:
        """List all handovers for a project."""
        return self._handover_repo.list_for_project(project)
