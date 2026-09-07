from typing import Any

import requests
import structlog

logger = structlog.get_logger(__name__)


def execute(
    session: requests.Session,
    url: str,
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a GraphQL request and handle common failures."""
    try:
        response = session.post(
            url,
            json={
                "query": query,
                "variables": variables or {},
            },
        )
        response.raise_for_status()
        result = response.json()
    except requests.exceptions.RequestException:
        logger.exception("graphql_request_failed")
        raise

    if "errors" in result:
        logger.error("graphql_request_failed", errors=result["errors"])
        raise RuntimeError(f"GraphQL Error: {result['errors']}")

    return result
