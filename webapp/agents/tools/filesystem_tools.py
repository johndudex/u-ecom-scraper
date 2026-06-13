"""Filesystem tools for LangGraph agent nodes.

All tools enforce a project-root sandbox — paths that resolve outside
``project_root`` are rejected with a clear error message.

Tools use the ``@tool`` decorator from ``langchain_core.tools`` so they are
automatically converted into LangChain ``BaseTool`` instances with correct
schemas for the LLM.
"""

import fnmatch
import glob
import logging
import os
import re
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _resolve_project_root(project_root: Optional[str] = None) -> str:
    """Return the effective project root directory."""
    if project_root:
        return os.path.abspath(project_root)
    try:
        from django.conf import settings

        if hasattr(settings, "PROJECT_ROOT"):
            return str(settings.PROJECT_ROOT)
    except Exception:
        pass
    return os.getcwd()


def _enforce_root(path: str, root: str) -> str:
    """Resolve *path* to absolute and verify it is inside *root*.

    Relative paths are resolved against *root*, not the current working
    directory.  Returns the resolved absolute path.

    Raises:
        ValueError: If the resolved path escapes the project root.
    """
    root_abs = os.path.abspath(root)
    if os.path.isabs(path):
        resolved = os.path.abspath(path)
    else:
        resolved = os.path.abspath(os.path.join(root_abs, path))
    if not resolved.startswith(root_abs + os.sep) and resolved != root_abs:
        raise ValueError(
            f"Path '{path}' resolves to '{resolved}' which is outside "
            f"the project root '{root_abs}'"
        )
    return resolved


def get_filesystem_tools(
    project_root: Optional[str] = None,
    workspace_scope: Optional[str] = None,
) -> list:
    """Return all filesystem tools with sandboxing configured.

    Args:
        project_root: Root directory to restrict file operations to.
            Falls back to ``settings.PROJECT_ROOT`` then ``os.getcwd()``.
        workspace_scope: If set, restrict search_files and search_content
            to only the ``workspace/{workspace_scope}/`` subdirectory.
            Read/write/edit still work on any path under project_root.

    Returns:
        List of LangChain BaseTool instances.
    """
    root = _resolve_project_root(project_root)

    if workspace_scope:
        ws = os.path.join(root, "workspace", workspace_scope)
        if not (os.path.isdir(ws) or os.path.isdir(os.path.dirname(ws))):
            logger.warning(
                "workspace_scope='%s' but %s does not exist — scoping to root",
                workspace_scope,
                ws,
            )

    @tool
    def read_file(path: str) -> str:
        """Read the content of a file and return it as a string.

        Args:
            path: Absolute or relative path to the file within the project.

        Returns:
            The file content as text, or an error message if the file
            cannot be read.
        """
        try:
            safe = _enforce_root(path, root)
        except ValueError as e:
            return str(e)
        try:
            with open(safe, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return f"File not found: {path}"
        except IsADirectoryError:
            return f"Path is a directory, not a file: {path}"
        except UnicodeDecodeError:
            return f"Cannot read binary file as text: {path}"
        except Exception as e:
            return f"Error reading '{path}': {e}"

    @tool
    def write_file(path: str, content: str) -> str:
        """Write content to a file, creating parent directories if needed.

        Args:
            path: Absolute or relative path within the project.
            content: Text content to write.

        Returns:
            Success message with the resolved path, or an error message.
        """
        try:
            safe = _enforce_root(path, root)
        except ValueError as e:
            return str(e)
        try:
            os.makedirs(os.path.dirname(safe) or ".", exist_ok=True)
            with open(safe, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Successfully wrote {len(content)} characters to {safe}"
        except Exception as e:
            return f"Error writing '{path}': {e}"

    @tool
    def edit_file(path: str, old_string: str, new_string: str) -> str:
        """Replace an exact string in a file with a new string.

        The replacement is literal — no regex.  If *old_string* is not found,
        or is found multiple times, the operation fails so the LLM can retry
        with a more specific match.

        Args:
            path: Absolute or relative path within the project.
            old_string: The exact text to find in the file.
            new_string: The replacement text.

        Returns:
            Success or failure message with details.
        """
        try:
            safe = _enforce_root(path, root)
        except ValueError as e:
            return str(e)
        try:
            with open(safe, "r", encoding="utf-8") as f:
                original = f.read()
        except FileNotFoundError:
            return f"File not found: {path}"
        except Exception as e:
            return f"Error reading '{path}' for editing: {e}"

        count = original.count(old_string)
        if count == 0:
            return (
                f"old_string not found in '{path}'. "
                "Provide a more specific match or check the file content."
            )
        if count > 1:
            return (
                f"old_string found {count} times in '{path}'. "
                "Provide more surrounding context to make the match unique."
            )

        updated = original.replace(old_string, new_string, 1)
        try:
            with open(safe, "w", encoding="utf-8") as f:
                f.write(updated)
            return (
                f"Successfully replaced 1 occurrence in {safe} "
                f"({len(original)} → {len(updated)} chars)"
            )
        except Exception as e:
            return f"Error writing edited file '{path}': {e}"

    @tool
    def search_files(pattern: str, path: str = ".") -> str:
        """Find files matching a glob pattern within the project.

        Args:
            pattern: Glob pattern (e.g. ``**/*.py``, ``src/**/*.json``).
            path: Base directory to search in. Defaults to the agent's
                workspace subfolder if scoping is active, otherwise project root.

        Returns:
            Newline-separated list of matching file paths, or an error message.
        """
        if workspace_scope and path == ".":
            effective_path = os.path.join("workspace", workspace_scope)
        else:
            effective_path = path
        try:
            base = _enforce_root(effective_path, root)
        except ValueError as e:
            return str(e)
        try:
            matches = sorted(
                glob.glob(os.path.join(base, pattern), recursive=True)
            )
            if not matches:
                return f"No files match pattern '{pattern}' in '{effective_path}'"
            rel = [os.path.relpath(m, root) for m in matches]
            return "\n".join(rel)
        except Exception as e:
            return f"Error searching files: {e}"

    @tool
    def search_content(
        pattern: str,
        path: str = ".",
        include: Optional[str] = None,
    ) -> str:
        """Search file contents with a regular expression.

        Args:
            pattern: Regex pattern to search for (e.g. ``class.*Scraper``).
            path: Base directory to search in. Defaults to the agent's
                workspace subfolder if scoping is active, otherwise project root.
            include: Optional file glob filter (e.g. ``*.py``).

        Returns:
            Matching files with line numbers and excerpt lines, or a message
            if nothing was found.
        """
        if workspace_scope and path == ".":
            effective_path = os.path.join("workspace", workspace_scope)
        else:
            effective_path = path
        try:
            base = _enforce_root(effective_path, root)
        except ValueError as e:
            return str(e)

        try:
            compiled = re.compile(pattern)
        except re.error as e:
            return f"Invalid regex pattern '{pattern}': {e}"

        results: list[str] = []
        try:
            for dirpath, _dirnames, filenames in os.walk(base):
                for fname in filenames:
                    if include and not fnmatch.fnmatch(fname, include):
                        continue
                    fpath = os.path.join(dirpath, fname)
                    rel = os.path.relpath(fpath, root)
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                            for lineno, line in enumerate(f, 1):
                                if compiled.search(line):
                                    excerpt = line.rstrip()[:200]
                                    results.append(f"{rel}:{lineno}: {excerpt}")
                    except Exception:
                        continue
        except Exception as e:
            return f"Error searching content: {e}"

        if not results:
            return f"No matches for pattern '{pattern}' in '{effective_path}'"
        return "\n".join(results)

    return [read_file, write_file, edit_file, search_files, search_content]
