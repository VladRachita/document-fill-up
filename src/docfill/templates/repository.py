"""Database access for standard documents."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from docfill.errors import TemplateNotFoundError
from docfill.templates.models import Base, StandardDocument, StandardDocumentSpec


def make_engine(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    import docfill.knowledge.store  # noqa: F401  (registers the knowledge tables)
    import docfill.learning  # noqa: F401  (registers the learning tables on the same metadata)

    Base.metadata.create_all(engine)
    _migrate(engine)
    return engine


def _migrate(engine: Engine) -> None:
    """Add columns introduced after a database was created (tiny, additive migrations)."""
    columns = {column["name"] for column in inspect(engine).get_columns("standard_documents")}
    if "options" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE standard_documents ADD COLUMN options JSON"))


def make_session_factory(database_url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=make_engine(database_url), expire_on_commit=False)


class TemplateRepository:
    def __init__(self, session: Session):
        self.session = session

    def list(self) -> list[StandardDocument]:
        return list(self.session.scalars(select(StandardDocument).order_by(StandardDocument.name)))

    def find(self, name: str) -> StandardDocument | None:
        return self.session.scalar(select(StandardDocument).where(StandardDocument.name == name))

    def get(self, name: str) -> StandardDocument:
        document = self.find(name)
        if document is None:
            raise TemplateNotFoundError(f"Standard document '{name}' not found")
        return document

    def save(self, spec: StandardDocumentSpec) -> tuple[StandardDocument, bool]:
        """Create or update a standard document. Returns ``(document, changed)``.

        Updating bumps the version; saving identical content is a no-op.
        """
        checksum = spec.checksum()
        document = self.find(spec.name)
        if document is not None and document.checksum == checksum:
            if document.description != spec.description:
                document.description = spec.description
                self.session.commit()
            return document, False
        if document is None:
            document = StandardDocument(name=spec.name, version=1)
            self.session.add(document)
        else:
            document.version += 1
        document.title = spec.title
        document.description = spec.description
        document.kind = spec.kind
        document.body = spec.body
        document.pdf_data = spec.pdf_data
        document.field_map = dict(spec.field_map)
        document.optional_fields = list(spec.optional_fields)
        document.options = spec.options()
        document.checksum = checksum
        self.session.commit()
        return document, True

    def delete(self, name: str) -> None:
        self.session.delete(self.get(name))
        self.session.commit()
