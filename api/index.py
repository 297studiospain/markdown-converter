"""Vercel entry point for the Flask conversion API.

Vercel detects the exported WSGI ``app`` and serves it as a Python Function.
The routing rule in ``vercel.json`` sends every ``/api/*`` request here, while
the Flask routes themselves remain defined in the shared local API module.
"""

from app import app
