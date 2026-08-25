import importlib.metadata as metadata
import importlib.util
import sys

print(f"EXECUTABLE={sys.executable}")
print(f"VERSION={sys.version}")
print(f"PREFIX={sys.prefix}")
print(f"BASE_PREFIX={sys.base_prefix}")
print("SYS_PATH_BEGIN")
for entry in sys.path:
    print(entry)
print("SYS_PATH_END")
print("DISTRIBUTIONS_BEGIN")
rows = []
for dist in metadata.distributions():
    name = dist.metadata.get("Name") or dist.metadata.get("Summary") or "<unnamed>"
    rows.append((name.lower(), name, dist.version, str(dist.locate_file(""))))
for _, name, version, location in sorted(rows):
    print(f"{name}\t{version}\t{location}")
print("DISTRIBUTIONS_END")
print("SELECTED_PACKAGES_BEGIN")
for package in [
    "harbor", "openai", "anthropic", "litellm", "langchain", "langchain-core",
    "langchain-openai", "langgraph", "requests", "httpx", "pydantic", "mcp",
    "instructor", "openai-agents", "autogen-agentchat", "crewai", "dspy",
    "smolagents", "llama-index-core", "google-genai", "groq", "together", "ollama",
]:
    try:
        print(f"{package}\tPRESENT\t{metadata.version(package)}")
    except metadata.PackageNotFoundError:
        print(f"{package}\tABSENT")
print("SELECTED_PACKAGES_END")
for module in ["harbor", "openai", "anthropic", "litellm", "langchain", "requests", "httpx", "pydantic"]:
    spec = importlib.util.find_spec(module)
    print(f"MODULE\t{module}\t{spec.origin if spec else 'ABSENT'}")
