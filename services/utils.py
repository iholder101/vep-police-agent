import os
from datetime import datetime
from typing import Any

from langgraph.graph.state import CompiledStateGraph

def get_api_key() -> str:
    """Read and return the API key from the API_KEY file."""
    with open("../API_KEY", "r") as f:
        return f.read().strip()

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
