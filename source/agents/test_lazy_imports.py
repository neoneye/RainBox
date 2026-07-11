"""Agent process startup must not pay the ~0.6s llama_index import before it can
post progress — llama_index loads lazily on the first LLM call, not at import."""
import subprocess
import sys
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent.parent


def _llama_index_loaded_after(import_stmt: str) -> bool:
    out = subprocess.run(
        [sys.executable, "-c",
         f"{import_stmt}; import sys; "
         "print(any(m.startswith('llama_index') for m in sys.modules))"],
        capture_output=True, text=True, cwd=SOURCE_DIR,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip() == "True"


def test_importing_agent_base_does_not_load_llama_index():
    assert not _llama_index_loaded_after("import agents.base")


def test_importing_assistant_does_not_load_llama_index():
    assert not _llama_index_loaded_after("import agents.assistant")


def test_resolve_agent_class_loads_only_the_selected_agent():
    """A spawned agent process must resolve only its own class — running the
    assistant must not import all 20 agents (and their llama_index) at startup."""
    out = subprocess.run(
        [sys.executable, "-c",
         "from agents.config import resolve_agent_class; "
         "cls = resolve_agent_class('assistant'); "
         "import sys; print(cls.__name__); "
         "print(any(m.startswith('llama_index') for m in sys.modules)); "
         "print('agents.query_filter_router' in sys.modules)"],
        capture_output=True, text=True, cwd=SOURCE_DIR,
    )
    assert out.returncode == 0, out.stderr
    name, llama, other_agent = out.stdout.split()
    assert name == "AssistantAgent"
    assert llama == "False", "resolving the assistant must not load llama_index"
    assert other_agent == "False", "resolving the assistant must not import other agents"
