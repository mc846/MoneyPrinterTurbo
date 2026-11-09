"""API controllers.

This file makes ``app.controllers`` a regular package. As a namespace package (no
``__init__.py``) Streamlit's source watcher watched the folder itself, and on
Windows an event for a module in a subfolder (``manager/``, ``v1/``) was compared
with the folder's own timestamp and listing. These never match, so reading such a
file, which updates its last-access time, counted as an edit and made the WebUI
re-import every application module (see ``app/utils/process_objects.py``).
"""
