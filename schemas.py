"""Schemas for the Defuddle-first web fetch router plugin."""

WEB_FETCH_SCHEMA = {
    "name": "web_fetch",
    "description": (
        "Fetch and extract content from a known URL using the plugin's routing policy. "
        "Use this FIRST for known URLs, articles, documentation pages, and normal web pages. "
        "Routing is enforced in code: raw/API/plain-text endpoints use direct HTTP, "
        "normal pages use local Defuddle on HTML fetched with public-IP and redirect checks. "
        "Remote fetching is disabled because remote redirect destinations cannot be validated. "
        "For search/discovery use web_search. For rendered pages, login or forms use browser tools."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute http(s) URL to fetch. This tool is for known URLs, not search queries.",
            },
            "mode": {
                "type": "string",
                "enum": ["auto", "article", "raw", "rendered"],
                "description": (
                    "Routing hint. auto chooses from URL shape. article forces Defuddle-first. "
                    "raw forces direct HTTP. Legacy rendered mode returns browser guidance without fetching."
                ),
                "default": "auto",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum extracted content characters to return. Default 50000.",
                "default": 50000,
            },
            "timeout": {
                "type": "integer",
                "description": "Per-method timeout in seconds. Default 30.",
                "default": 30,
            },
            "ttl": {
                "type": "integer",
                "description": "Deprecated compatibility parameter; ignored (remote fetching is disabled).",
            },
        },
        "required": ["url"],
    },
}
