import logging
import math
import re

import numexpr
from ddgs import DDGS
from langchain_chroma import Chroma
from langchain_core.tools import BaseTool, tool

from core import get_embeddings

logger = logging.getLogger(__name__)


def calculator_func(expression: str) -> str:
    """Calculates a math expression using numexpr.

    Useful for when you need to answer questions about math using numexpr.
    This tool is only for math questions and nothing else. Only input
    math expressions.

    Args:
        expression (str): A valid numexpr formatted math expression.

    Returns:
        str: The result of the math expression.
    """

    try:
        local_dict = {"pi": math.pi, "e": math.e}
        output = str(
            numexpr.evaluate(
                expression.strip(),
                global_dict={},  # restrict access to globals
                local_dict=local_dict,  # add common mathematical functions
            )
        )
        return re.sub(r"^\[|\]$", "", output)
    except Exception as e:
        raise ValueError(
            f'calculator("{expression}") raised error: {e}.'
            " Please try again with a valid numerical expression"
        )


calculator: BaseTool = tool(calculator_func)
calculator.name = "Calculator"


# Format retrieved documents
def format_contexts(docs):
    return "\n\n".join(doc.page_content for doc in docs)


def load_chroma_db():
    # Create the embedding function for our project description database
    try:
        embeddings = get_embeddings()
    except Exception as e:
        raise RuntimeError(
            "Failed to initialize the embeddings backend. Check OPENAI_API_KEY, or "
            "EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL if you are using an "
            "OpenAI-compatible provider."
        ) from e

    # Load the stored vector database
    chroma_db = Chroma(persist_directory="./chroma_db", embedding_function=embeddings)
    retriever = chroma_db.as_retriever(search_kwargs={"k": 5})
    return retriever


def database_search_func(query: str) -> str:
    """Searches chroma_db for information in the company's handbook."""
    # Get the chroma retriever
    retriever = load_chroma_db()

    # Search the database for relevant documents
    documents = retriever.invoke(query)

    # Format the documents into a string
    context_str = format_contexts(documents)

    return context_str


database_search: BaseTool = tool(database_search_func)
database_search.name = "Database_Search"  # Update name with the purpose of your database


# ddgs' own default is too short on this network: engines that do answer need up to ~30s
# when the request goes through a proxy, and get killed early otherwise.
SEARCH_TIMEOUT = 30


def web_search_func(query: str) -> str:
    """Searches the web for current information.

    Useful for when you need to answer questions about current events, news,
    weather or anything else not covered by your training data.

    Args:
        query (str): The search query.

    Returns:
        str: The top search results, or a message explaining why there are none.
    """

    try:
        results = DDGS(timeout=SEARCH_TIMEOUT).text(query, max_results=5)
    except Exception as e:
        logger.warning(f"web search failed for {query!r}: {e}")
        raise
    if not results:
        return "No results found."
    return "\n\n".join(
        f"{result.get('title', '')}\n{result.get('href', '')}\n{result.get('body', '')}"
        for result in results
    )


web_search: BaseTool = tool(web_search_func)
web_search.name = "WebSearch"
