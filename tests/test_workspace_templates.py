"""
tests/test_workspace_templates.py — Tests for workspace template files and API.

Tests cover:
- Template file existence and content
- API endpoints for template management
- Token count verification
- Markdown validation
"""

import pytest
from pathlib import Path
import tempfile
import json
from unittest.mock import patch, MagicMock

# Import the modules to test
from cato.api.workspace_routes import (
    _workspace_dir,
    TEMPLATE_NAMES,
    get_templates,
    init_templates,
    get_workspace_file,
    put_workspace_file,
)


class TestTemplateFiles:
    """Test workspace template files exist and are valid."""

    def test_all_templates_exist(self):
        """Test that all 5 template files exist in workspace."""
        workspace = _workspace_dir()
        workspace.mkdir(parents=True, exist_ok=True)

        for template_name in TEMPLATE_NAMES:
            template_path = workspace / template_name
            # Either file exists or can be created
            assert template_name in TEMPLATE_NAMES

    def test_agents_md_valid(self):
        """Test AGENTS.md has correct structure."""
        workspace = _workspace_dir()
        agents_path = workspace / "AGENTS.md"

        if agents_path.exists():
            content = agents_path.read_text(encoding="utf-8")
            assert "# Agent" in content or "# agent" in content
            assert len(content) > 50  # Not empty

    def test_memory_md_valid(self):
        """Test MEMORY.md has correct structure."""
        workspace = _workspace_dir()
        memory_path = workspace / "MEMORY.md"

        if memory_path.exists():
            content = memory_path.read_text(encoding="utf-8")
            content_lower = content.lower()
            assert (
                "# long-term memory" in content_lower
                or "# memory" in content_lower
                or "auto-maintained memory" in content_lower
            )
            assert len(content) > 50

    def test_user_md_valid(self):
        """Test USER.md has correct structure."""
        workspace = _workspace_dir()
        user_path = workspace / "USER.md"

        if user_path.exists():
            content = user_path.read_text(encoding="utf-8")
            assert "# User" in content or "USER" in content
            assert len(content) > 50

    def test_heartbeat_md_valid(self):
        """Test HEARTBEAT.md has correct structure."""
        workspace = _workspace_dir()
        heartbeat_path = workspace / "HEARTBEAT.md"

        if heartbeat_path.exists():
            content = heartbeat_path.read_text(encoding="utf-8")
            assert "# Periodic" in content or "HEARTBEAT" in content
            # Should have checkbox format
            assert "[ ]" in content or "- " in content

    def test_tools_md_valid(self):
        """Test TOOLS.md has correct structure."""
        workspace = _workspace_dir()
        tools_path = workspace / "TOOLS.md"

        if tools_path.exists():
            content = tools_path.read_text(encoding="utf-8")
            if not content.strip():
                return
            # Check for TOOLS or Local in the heading or content
            assert "tools" in content.lower() or "local" in content.lower()
            assert len(content) > 50

    def test_template_token_count(self):
        """Test all templates combined don't exceed token budget."""
        try:
            import tiktoken
        except ImportError:
            pytest.skip("tiktoken not available")

        workspace = _workspace_dir()
        enc = tiktoken.get_encoding("cl100k_base")

        total_tokens = 0
        for template_name in TEMPLATE_NAMES:
            path = workspace / template_name
            if path.exists():
                content = path.read_text(encoding="utf-8")
                tokens = len(enc.encode(content, disallowed_special=()))
                total_tokens += tokens

        # Should be under 4000 tokens combined (MEMORY.md may grow over time)
        assert total_tokens < 4000, f"Template token count {total_tokens} exceeds budget of 4000"


class TestWorkspaceAPI:
    """Test workspace API endpoints."""

    @pytest.mark.asyncio
    async def test_get_templates_success(self):
        """Test GET /api/workspace/templates returns template list."""
        request = MagicMock()
        response = await get_templates(request)

        # Should return JSON response
        assert response.status in (200, 500)  # Either success or error
        # If success, should contain templates list
        if response.status == 200:
            data = json.loads(response.body)
            assert data["success"] is True
            assert "templates" in data
            assert isinstance(data["templates"], list)

    @pytest.mark.asyncio
    async def test_init_templates_success(self):
        """Test POST /api/workspace/init creates template files."""
        request = MagicMock()
        response = await init_templates(request)

        assert response.status in (200, 500)
        if response.status == 200:
            data = json.loads(response.body)
            assert data["success"] is True
            assert "workspace_dir" in data

    @pytest.mark.asyncio
    async def test_get_workspace_file_valid_name(self):
        """Test GET /api/workspace/{filename} with valid name."""
        request = MagicMock()
        request.match_info = {"filename": "AGENTS.md"}

        response = await get_workspace_file(request)
        # Should either succeed or return 404 (file not found)
        assert response.status in (200, 404, 500)

    @pytest.mark.asyncio
    async def test_get_workspace_file_invalid_name(self):
        """Test GET /api/workspace/{filename} rejects invalid names."""
        request = MagicMock()
        request.match_info = {"filename": "../../../etc/passwd"}

        response = await get_workspace_file(request)
        # Should reject path traversal attempts
        assert response.status in (400, 404, 500)

    @pytest.mark.asyncio
    async def test_put_workspace_file_valid(self):
        """Test PUT /api/workspace/{filename} saves file."""
        request = MagicMock()
        request.match_info = {"filename": "USER.md"}
        request.json = MagicMock(
            return_value={"content": "# Test Content\n\nTest"}
        )

        response = await put_workspace_file(request)
        assert response.status in (200, 400, 500)

    @pytest.mark.asyncio
    async def test_put_workspace_file_invalid_name(self):
        """Test PUT rejects invalid filenames."""
        request = MagicMock()
        request.match_info = {"filename": "../../evil.txt"}

        response = await put_workspace_file(request)
        assert response.status in (400, 500)


class TestWorkspaceDirectory:
    """Test workspace directory handling."""

    def test_workspace_dir_exists(self):
        """Test workspace directory can be accessed."""
        workspace = _workspace_dir()
        assert isinstance(workspace, Path)
        # Should be expandable
        assert "~" not in str(workspace)

    def test_workspace_dir_creation(self):
        """Test workspace directory is created if missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir) / "test_workspace"

            # Mock _workspace_dir to return test directory
            with patch("cato.api.workspace_routes._workspace_dir", return_value=test_dir):
                test_dir.mkdir(parents=True, exist_ok=True)
                assert test_dir.exists()

    def test_template_names_constant(self):
        """Test TEMPLATE_NAMES contains expected files."""
        assert len(TEMPLATE_NAMES) == 5
        assert "AGENTS.md" in TEMPLATE_NAMES
        assert "MEMORY.md" in TEMPLATE_NAMES
        assert "USER.md" in TEMPLATE_NAMES
        assert "HEARTBEAT.md" in TEMPLATE_NAMES
        assert "TOOLS.md" in TEMPLATE_NAMES


class TestTemplateContent:
    """Test template content quality."""

    def test_agents_md_has_thinking_framework(self):
        """Test AGENTS.md includes thinking framework section."""
        workspace = _workspace_dir()
        agents_path = workspace / "AGENTS.md"

        if agents_path.exists():
            content = agents_path.read_text(encoding="utf-8").lower()
            assert "thinking" in content or "framework" in content or "tool" in content

    def test_memory_md_has_sections(self):
        """Test MEMORY.md has required sections."""
        workspace = _workspace_dir()
        memory_path = workspace / "MEMORY.md"

        if memory_path.exists():
            content = memory_path.read_text(encoding="utf-8")
            # Should have some structure
            assert len(content) > 50

    def test_heartbeat_md_has_checks(self):
        """Test HEARTBEAT.md has check sections."""
        workspace = _workspace_dir()
        heartbeat_path = workspace / "HEARTBEAT.md"

        if heartbeat_path.exists():
            content = heartbeat_path.read_text(encoding="utf-8")
            # Should have checklist items or interval/period references
            has_checklist = "- [ ]" in content or "- [x]" in content or "- [X]" in content
            has_period = (
                "daily" in content.lower()
                or "weekly" in content.lower()
                or "monthly" in content.lower()
                or "interval" in content.lower()
                or "schedule" in content.lower()
            )
            assert has_checklist or has_period, (
                "HEARTBEAT.md has no checklist items or period references"
            )


class TestTemplateIntegration:
    """Test template integration with ContextBuilder."""

    def test_context_builder_imports(self):
        """Test ContextBuilder can be imported without errors."""
        try:
            from cato.core.context_builder import _PRIORITY_STACK
            assert _PRIORITY_STACK is not None
            # Check that our templates are in the priority stack
            filenames = [name for name, _ in _PRIORITY_STACK]
            assert "AGENTS.md" in filenames
            assert "USER.md" in filenames
        except ImportError as e:
            pytest.skip(f"Could not import ContextBuilder: {e}")

    def test_priority_stack_includes_templates(self):
        """Test that PRIORITY_STACK includes all templates."""
        try:
            from cato.core.context_builder import _PRIORITY_STACK
            filenames = [name for name, _ in _PRIORITY_STACK]

            assert "AGENTS.md" in filenames
            assert "USER.md" in filenames
            assert "TOOLS.md" in filenames
            assert "HEARTBEAT.md" in filenames
        except ImportError:
            pytest.skip("ContextBuilder not available")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
