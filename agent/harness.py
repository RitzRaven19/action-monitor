"""LangGraph agent harness (Phase 0).

Builds a small ReAct-style graph: call_model -> tools -> call_model -> ... -> END.
The model is Groq (free tier), authenticated via GROQ_API_KEY.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import groq
import httpx
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langgraph.graph import END, StateGraph, MessagesState
from langgraph.prebuilt import ToolNode, tools_condition
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from agent.tools import build_tools
from logger.action_logger import ActionLogger

load_dotenv()

DEFAULT_MODEL = "openai/gpt-oss-120b"

SYSTEM_PROMPT = (
    "You are a helpful assistant with access to tools. Use only the tools you "
    "are given, and only as needed to complete the user's task. Do not call "
    "web_search unless the task explicitly asks you to search or look something "
    "up online -- reading a local file is not a reason to search the web. If a "
    "tool call fails or returns something unclear, do not retry with different "
    "queries; proceed with what you have. Be concise and make the minimum "
    "number of tool calls needed to complete the task."
)


@retry(
    retry=retry_if_exception_type((groq.RateLimitError, groq.APIStatusError, groq.APIConnectionError, httpx.TransportError)),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    stop=stop_after_attempt(6),
    reraise=True,
)
def _invoke_with_retry(llm_with_tools, messages):
    """Free tiers throw transient 429 (rate-limit) and 5xx (server overloaded)
    errors under normal use, and the underlying httpx client can hit transient
    DNS/connection errors; retry with backoff rather than failing the whole task
    run over what is usually a temporary condition."""
    return llm_with_tools.invoke(messages)


def build_agent(
    logger: ActionLogger,
    include_network_post: bool = False,
    model_name: str = DEFAULT_MODEL,
    checkpointer=None,
):
    """Construct and compile the LangGraph agent, wired to `logger` via wrapped tools.

    `checkpointer` (e.g. langgraph.checkpoint.memory.MemorySaver) is optional and
    defaults to None, which is the current behavior everywhere: no persistence,
    every call independent. Passing a checkpointer plus a `thread_id` in the
    invoke/stream config gives the agent real multi-turn memory -- LangGraph
    merges each turn's new messages onto that thread's persisted state via
    MessagesState's own reducer, so callers only ever need to send the new
    turn's message, not the whole history.
    """
    if not os.environ.get("GROQ_API_KEY"):
        raise RuntimeError(
            "GROQ_API_KEY is not set. Copy .env.example to .env and fill in your free "
            "Groq key (console.groq.com/keys)."
        )

    tools = build_tools(logger, include_network_post=include_network_post)
    llm = ChatGroq(model=model_name, temperature=0)
    llm_with_tools = llm.bind_tools(tools)

    def call_model(state: MessagesState) -> dict[str, Any]:
        response = _invoke_with_retry(llm_with_tools, state["messages"])
        return {"messages": [response]}

    graph = StateGraph(MessagesState)
    graph.add_node("call_model", call_model)
    graph.add_node("tools", ToolNode(tools))
    graph.set_entry_point("call_model")
    graph.add_conditional_edges("call_model", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "call_model")

    return graph.compile(checkpointer=checkpointer)


def run_task(task_prompt: str, logger: ActionLogger, include_network_post: bool = False) -> dict:
    """Run a single task through the agent. Returns the final state (messages)."""
    app = build_agent(logger, include_network_post=include_network_post)
    result = app.invoke(
        {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task_prompt},
            ]
        },
        config={"recursion_limit": 25},
    )
    return result


if __name__ == "__main__":
    import sys

    prompt = sys.argv[1] if len(sys.argv) > 1 else "Read data/sample_notes.txt and summarize it in two sentences."
    log = ActionLogger(Path(__file__).resolve().parent.parent / "logs" / "manual_run.jsonl")
    final_state = run_task(prompt, log)
    print("--- Final message ---")
    print(final_state["messages"][-1].content)
    print("--- Action log ---")
    for rec in log.read_all():
        print(rec)
