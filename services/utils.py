import os
from datetime import datetime
from typing import Any

from langgraph.graph.state import CompiledStateGraph

def get_api_key() -> str:
    """Read and return the API key from the API_KEY file."""
    # Try current directory first, then parent (for symlink support)
    import os
    api_key_path = "API_KEY"
    if not os.path.exists(api_key_path):
        api_key_path = "../API_KEY"
    with open(api_key_path, "r") as f:
        return f.read().strip()


def get_google_token() -> str:
    """Read and return the Google token from the GOOGLE_TOKEN file.
    
    The token should be a JSON string containing Google service account credentials.
    """
    import os
    # Try current directory first, then parent (for symlink support)
    token_path = "GOOGLE_TOKEN"
    if not os.path.exists(token_path):
        token_path = "../GOOGLE_TOKEN"
    
    try:
        with open(token_path, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        raise FileNotFoundError(
            "GOOGLE_TOKEN file not found. Please create GOOGLE_TOKEN file with your Google service account credentials (JSON)."
        )

def invoke_agent(agent: CompiledStateGraph, prompt: str) -> str:
    response = agent.invoke(
        {"messages": [{"role": "user", "content": prompt}]}
    )

    content = response["messages"][-1].content

    # Handle list of content blocks (e.g., [{'type': 'text', 'text': '...'}])
    if isinstance(content, list):
        text_parts = [item.get("text", "") for item in content if isinstance(item, dict) and "text" in item]
        return "".join(text_parts) if text_parts else str(content)

    # Handle string content directly
    return str(content) if content is not None else ""

def get_model():
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model="gemini-3-flash-preview",
        api_key=get_api_key(),
        timeout=60,
    )


def log(message: str, node: str = "SYSTEM", level: str = "INFO") -> None:
    """Centralized logging function.
    
    Currently uses print, but can be easily switched to proper logging later.
    
    Args:
        message: Log message
        node: Node/component name (e.g., "scheduler", "fetch_data")
        level: Log level (INFO, WARNING, ERROR, DEBUG)
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level:5s}] [{node:15s}] {message}")
