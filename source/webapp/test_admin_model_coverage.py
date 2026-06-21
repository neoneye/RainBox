"""Every ORM model is registered in the Flask-Admin UI (so a newly-added model
shows up without anyone remembering to wire it). MemoryEmbedding's vector column
is excluded from its view."""

import db
import webapp  # noqa: F401 — registers all admin views
from webapp.core import admin


def _registered_models():
    return {getattr(v, "model", None) for v in admin._views} - {None}


def test_every_db_model_has_an_admin_view():
    registered = _registered_models()
    mapped = {m.class_ for m in db.db.Model.registry.mappers}
    missing = sorted(m.__name__ for m in mapped if m not in registered)
    assert not missing, f"db.Model classes with no admin view: {missing}"


def test_memory_embedding_view_hides_the_vector_column():
    view = next(v for v in admin._views if getattr(v, "model", None) is db.MemoryEmbedding)
    assert "embedding" in (view.column_exclude_list or ())
    assert view.can_edit is False  # machine-generated; read-only


def test_seed_memory_table_renamed():
    import db
    assert db.SeedMemoryKb.__tablename__ == "data_seed_memory"
    import memory.seed_memory as kb
    assert kb.QA_FULL_TABLE == "data_seed_memory"
