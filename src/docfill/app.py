"""Assemble the pipeline with its learning store (used by the CLI and the API)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from docfill.config import Settings, get_settings
from docfill.doctypes import DocTypeClassifier
from docfill.extraction import FieldExtractor
from docfill.knowledge import KnowledgeBase
from docfill.learning import LearningStore
from docfill.pipeline import DocFill
from docfill.templates import TemplateRepository, make_session_factory
from docfill.templates.models import StandardDocument


@dataclass
class App:
    settings: Settings
    sessions: sessionmaker[Session]
    learning: LearningStore
    docfill: DocFill
    knowledge: KnowledgeBase

    def templates(self) -> list[StandardDocument]:
        with self.sessions() as session:
            return TemplateRepository(session).list()

    def repository(self, session: Session) -> TemplateRepository:
        return TemplateRepository(session)


def build_app(settings: Settings | None = None, use_ner: bool = True) -> App:
    settings = settings or get_settings()
    sessions = make_session_factory(settings.database_url)
    learning = LearningStore(sessions)
    extractor = FieldExtractor(
        settings,
        use_ner=use_ner,
        calibrator=learning.calibrate,
        learned_labels=learning.learned_labels,
        fix_spelling=learning.fix_spelling,
    )

    def templates() -> list[StandardDocument]:
        with sessions() as session:
            return TemplateRepository(session).list()

    knowledge = KnowledgeBase(sessions, templates=lambda: [t.name for t in templates()])
    docfill = DocFill(
        settings,
        extractor,
        classifier=DocTypeClassifier(learning.doc_examples, knowledge.doc_types),
        templates=templates,
        memory=learning.remembered,
    )
    return App(settings, sessions, learning, docfill, knowledge)
