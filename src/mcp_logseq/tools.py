import os
import logging
from typing import Any
from . import logseq
from . import parser
from mcp.types import Tool, TextContent

logger = logging.getLogger("mcp-logseq")

api_key = os.getenv("LOGSEQ_API_TOKEN", "")
if api_key == "":
    raise ValueError("LOGSEQ_API_TOKEN environment variable required")
else:
    logger.info("Found LOGSEQ_API_TOKEN in environment")
    logger.debug(f"API Token starts with: {api_key[:5]}...")


class ToolHandler:
    def __init__(self, tool_name: str):
        self.name = tool_name

    def get_tool_description(self) -> Tool:
        raise NotImplementedError()

    def run_tool(self, args: dict) -> list[TextContent]:
        raise NotImplementedError()


# =============================================================================
# TOOL HANDLERS (with proper markdown parsing and block hierarchy)
# =============================================================================


class CreatePageToolHandler(ToolHandler):
    """
    Create a new page with proper block hierarchy.

    Parses markdown content into Logseq blocks, supporting:
    - Headings (# ## ###) with nested hierarchy
    - Bullet and numbered lists with nesting
    - Code blocks (fenced with ```)
    - Blockquotes (>)
    - YAML frontmatter for page properties
    """

    def __init__(self):
        super().__init__("create_page")

    def get_tool_description(self):
        return Tool(
            name=self.name,
            description="""Create a new page in Logseq with properly structured blocks.

Markdown content is automatically parsed into Logseq's block hierarchy:
- Headings (# ## ###) create nested sections
- Lists (- or 1.) become proper block trees  
- Code blocks are preserved as single blocks
- YAML frontmatter (---) becomes page properties

Example content:
```
---
tags: [project, active]
priority: high
---

# Project Title
Introduction paragraph.

## Tasks
- Task 1
  - Subtask A
- Task 2
```""",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Title of the new page"},
                    "content": {
                        "type": "string",
                        "description": "Markdown content to parse into blocks (optional)",
                    },
                    "properties": {
                        "type": "object",
                        "description": "Page properties (merged with frontmatter if both provided)",
                        "additionalProperties": True,
                    },
                },
                "required": ["title"],
            },
        )

    def run_tool(self, args: dict) -> list[TextContent]:
        if "title" not in args:
            raise RuntimeError("title argument required")

        title = args["title"]
        content = args.get("content", "")
        explicit_properties = args.get("properties", {})

        try:
            api = logseq.LogSeq(api_key=api_key)

            # Parse the content
            parsed = (
                parser.parse_content(content) if content else parser.ParsedContent()
            )

            # Merge properties: explicit properties override frontmatter
            page_properties = {**parsed.properties, **explicit_properties}

            # Convert blocks to batch format
            blocks = parsed.to_batch_format()

            # Create the page with blocks
            api.create_page_with_blocks(title, blocks, page_properties)

            # Build success message
            block_count = len(blocks)
            prop_count = len(page_properties)

            msg_parts = [f"Successfully created page '{title}'"]
            if block_count > 0:
                msg_parts.append(f"  - {block_count} top-level block(s) created")
            if prop_count > 0:
                msg_parts.append(f"  - {prop_count} page property/ies set")

            return [TextContent(type="text", text="\n".join(msg_parts))]
        except Exception as e:
            logger.error(f"Failed to create page: {str(e)}")
            raise


class ListPagesToolHandler(ToolHandler):
    def __init__(self):
        super().__init__("list_pages")

    def get_tool_description(self):
        return Tool(
            name=self.name,
            description="Lists all pages in a LogSeq graph.",
            inputSchema={
                "type": "object",
                "properties": {
                    "include_journals": {
                        "type": "boolean",
                        "description": "Whether to include journal/daily notes in the list",
                        "default": False,
                    }
                },
                "required": [],
            },
        )

    def run_tool(self, args: dict) -> list[TextContent]:
        include_journals = args.get("include_journals", False)

        try:
            api = logseq.LogSeq(api_key=api_key)
            result = api.list_pages()

            # Format pages for display
            pages_info = []
            for page in result:
                # Skip if it's a journal page and we don't want to include those
                is_journal = page.get("journal?", False)
                if is_journal and not include_journals:
                    continue

                # Get page information
                name = page.get("originalName") or page.get("name", "<unknown>")

                # Build page info string
                info_parts = [f"- {name}"]
                if is_journal:
                    info_parts.append("[journal]")

                pages_info.append(" ".join(info_parts))

            # Sort alphabetically by page name
            pages_info.sort()

            # Build response
            count_msg = f"\nTotal pages: {len(pages_info)}"
            journal_msg = (
                " (excluding journal pages)"
                if not include_journals
                else " (including journal pages)"
            )

            response = (
                "LogSeq Pages:\n\n" + "\n".join(pages_info) + count_msg + journal_msg
            )

            return [TextContent(type="text", text=response)]

        except Exception as e:
            logger.error(f"Failed to list pages: {str(e)}")
            raise


class GetPageContentToolHandler(ToolHandler):
    def __init__(self):
        super().__init__("get_page_content")

    @staticmethod
    def _format_block_tree(
        block: dict, indent_level: int = 0, max_depth: int = -1
    ) -> list[str]:
        """
        Recursively format a block and its children with proper indentation.

        Args:
            block: Block dict with 'content', 'children', and optional 'properties', 'marker'
            indent_level: Current indentation level (0-based)
            max_depth: Maximum depth to recurse (-1 for unlimited)

        Returns:
            List of formatted lines for this block and its children
        """
        lines = []

        # Get block content
        content = block.get("content", "").strip()
        if not content:
            return lines

        # Build the formatted line with indentation
        # Note: Properties are already included in the content by Logseq,
        # so we don't need to add them separately from block.properties
        indent = "  " * indent_level
        line = f"{indent}- {content}"
        lines.append(line)

        # Process children if we haven't hit the depth limit
        children = block.get("children", [])
        if children and (max_depth == -1 or indent_level < max_depth):
            for child in children:
                child_lines = GetPageContentToolHandler._format_block_tree(
                    child, indent_level + 1, max_depth
                )
                lines.extend(child_lines)

        return lines

    def get_tool_description(self):
        return Tool(
            name=self.name,
            description="Get the content of a specific page from LogSeq.",
            inputSchema={
                "type": "object",
                "properties": {
                    "page_name": {
                        "type": "string",
                        "description": "Name of the page to retrieve",
                    },
                    "format": {
                        "type": "string",
                        "description": "Output format (text or json)",
                        "enum": ["text", "json"],
                        "default": "text",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum nesting depth to display (default: -1 for unlimited)",
                        "default": -1,
                    },
                },
                "required": ["page_name"],
            },
        )

    def run_tool(self, args: dict) -> list[TextContent]:
        """Get and format LogSeq page content."""
        logger.info(f"Getting page content with args: {args}")

        if "page_name" not in args:
            raise RuntimeError("page_name argument required")

        try:
            api = logseq.LogSeq(api_key=api_key)
            result = api.get_page_content(args["page_name"])

            if not result:
                return [
                    TextContent(
                        type="text", text=f"Page '{args['page_name']}' not found."
                    )
                ]

            # Handle JSON format request
            if args.get("format") == "json":
                return [TextContent(type="text", text=str(result))]

            # Format as readable text
            content_parts = []

            # Get blocks from the result structure
            # Note: Page properties are already in the first block's content,
            # so we don't need to show them separately in YAML frontmatter
            blocks = result.get("blocks", [])
<<<<<<< HEAD

            # Blocks content - use recursive formatter
            max_depth = args.get("max_depth", -1)
            if blocks:
                for block in blocks:
                    if isinstance(block, dict):
                        block_lines = self._format_block_tree(block, 0, max_depth)
                        content_parts.extend(block_lines)
                    elif isinstance(block, str) and block.strip():
                        content_parts.append(f"- {block}")
            else:
                # Empty page - return single dash
                content_parts.append("-")

            return [TextContent(type="text", text="\n".join(content_parts))]

        except Exception as e:
            logger.error(f"Failed to get page content: {str(e)}")
            raise


class DeletePageToolHandler(ToolHandler):
    def __init__(self):
        super().__init__("delete_page")

    def get_tool_description(self):
        return Tool(
            name=self.name,
            description="Delete a page from LogSeq.",
            inputSchema={
                "type": "object",
                "properties": {
                    "page_name": {
                        "type": "string",
                        "description": "Name of the page to delete",
                    }
                },
                "required": ["page_name"],
            },
        )

    def run_tool(self, args: dict) -> list[TextContent]:
        if "page_name" not in args:
            raise RuntimeError("page_name argument required")

        try:
            api = logseq.LogSeq(api_key=api_key)
            result = api.delete_page(args["page_name"])

            # Build detailed success message
            page_name = args["page_name"]
            success_msg = f"✅ Successfully deleted page '{page_name}'"

            # Add any additional info from the API result if available
            if result and isinstance(result, dict):
                if result.get("success"):
                    success_msg += (
                        f"\n📋 Status: {result.get('message', 'Deletion confirmed')}"
                    )

            success_msg += (
                f"\n🗑️  Page '{page_name}' has been permanently removed from LogSeq"
            )

            return [TextContent(type="text", text=success_msg)]
        except ValueError as e:
            # Handle validation errors (page not found) gracefully
            return [TextContent(type="text", text=f"❌ Error: {str(e)}")]
        except Exception as e:
            logger.error(f"Failed to delete page: {str(e)}")
            return [
                TextContent(
                    type="text",
                    text=f"❌ Failed to delete page '{args['page_name']}': {str(e)}",
                )
            ]


class UpdatePageToolHandler(ToolHandler):
    """
    Update a page with proper block hierarchy support.

    Supports two modes:
    - append: Add new blocks after existing content (default)
    - replace: Clear existing content and add new blocks
    """

    def __init__(self):
        super().__init__("update_page")

    def get_tool_description(self):
        return Tool(
            name=self.name,
            description="""Update a page in Logseq with new content and/or properties.

Supports two modes:
- append: Add new blocks after existing content (default)
- replace: Clear all existing blocks and add new content

Markdown is parsed into proper block hierarchy just like create_page.
YAML frontmatter in content will be merged with explicit properties.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "page_name": {
                        "type": "string",
                        "description": "Name of the page to update",
                    },
                    "content": {
                        "type": "string",
                        "description": "Markdown content to add or replace with",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["append", "replace"],
                        "default": "append",
                        "description": "append: add after existing content. replace: clear page and add new content.",
                    },
                    "properties": {
                        "type": "object",
                        "description": "Page properties to set/update",
                        "additionalProperties": True,
                    },
                },
                "required": ["page_name"],
            },
        )

    def run_tool(self, args: dict) -> list[TextContent]:
        if "page_name" not in args:
            raise RuntimeError("page_name argument required")

        page_name = args["page_name"]
        content = args.get("content", "")
        mode = args.get("mode", "append")
        explicit_properties = args.get("properties", {})

        # Validate that at least one update is provided
        if not content and not explicit_properties:
            return [
                TextContent(
                    type="text",
                    text="Error: Either 'content' or 'properties' must be provided for update",
                )
            ]

        try:
            api = logseq.LogSeq(api_key=api_key)

            # Parse the content
            parsed = (
                parser.parse_content(content) if content else parser.ParsedContent()
            )

            # Merge properties: explicit properties override frontmatter
            page_properties = (
                {**parsed.properties, **explicit_properties}
                if (parsed.properties or explicit_properties)
                else None
            )

            # Convert blocks to batch format
            blocks = parsed.to_batch_format()

            # Update the page
            result = api.update_page_with_blocks(
                page_name, blocks, page_properties, mode=mode
            )

            # Build success message
            updates = result.get("updates", [])
            msg_parts = [f"Successfully updated page '{page_name}'"]

            for update_type, update_value in updates:
                if update_type == "cleared":
                    msg_parts.append("  - Existing content cleared")
                elif update_type == "properties":
                    msg_parts.append(f"  - {len(update_value)} property/ies updated")
                elif update_type == "blocks_replaced":
                    msg_parts.append(f"  - {update_value} block(s) added")
                elif update_type == "blocks_appended":
                    msg_parts.append(f"  - {update_value} block(s) appended")

            msg_parts.append(f"Mode: {mode}")

            return [TextContent(type="text", text="\n".join(msg_parts))]
        except ValueError as e:
            return [TextContent(type="text", text=f"Error: {str(e)}")]
        except Exception as e:
            logger.error(f"Failed to update page: {str(e)}")
            return [
                TextContent(
                    type="text", text=f"Failed to update page '{page_name}': {str(e)}"
                )
            ]


class SearchToolHandler(ToolHandler):
    def __init__(self):
        super().__init__("search")

    def get_tool_description(self):
        return Tool(
            name=self.name,
            description="Search for content across LogSeq pages, blocks, and files",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query text"},
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return",
                        "default": 20,
                    },
                    "include_blocks": {
                        "type": "boolean",
                        "description": "Include block content results",
                        "default": True,
                    },
                    "include_pages": {
                        "type": "boolean",
                        "description": "Include page name results",
                        "default": True,
                    },
                    "include_files": {
                        "type": "boolean",
                        "description": "Include file name results",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
        )

    def run_tool(self, args: dict) -> list[TextContent]:
        """Execute search and format results."""
        logger.info(f"Searching with args: {args}")

        if "query" not in args:
            raise RuntimeError("query argument required")

        query = args["query"]
        limit = args.get("limit", 20)
        include_blocks = args.get("include_blocks", True)
        include_pages = args.get("include_pages", True)
        include_files = args.get("include_files", False)

        try:
            # Prepare search options
            search_options = {"limit": limit}

            api = logseq.LogSeq(api_key=api_key)
            result = api.search_content(query, search_options)

            if not result:
                return [
                    TextContent(
                        type="text", text=f"No search results found for '{query}'"
                    )
                ]

            # Format results
            content_parts = []
            content_parts.append(f"# Search Results for '{query}'\n")

            # Block results
            if include_blocks and result.get("blocks"):
                blocks = result["blocks"]
                content_parts.append(f"## 📄 Content Blocks ({len(blocks)} found)")
                for i, block in enumerate(blocks[:limit]):
                    # LogSeq returns blocks with 'block/content' key
                    content = block.get("block/content", "").strip()
                    if content:
                        # Truncate long content
                        if len(content) > 150:
                            content = content[:150] + "..."
                        content_parts.append(f"{i + 1}. {content}")
                content_parts.append("")

            # Page snippet results
            if include_blocks and result.get("pages-content"):
                snippets = result["pages-content"]
                content_parts.append(f"## 📝 Page Snippets ({len(snippets)} found)")
                for i, snippet in enumerate(snippets[:limit]):
                    # LogSeq returns snippets with 'block/snippet' key
                    snippet_text = snippet.get("block/snippet", "").strip()
                    if snippet_text:
                        # Clean up snippet text
                        snippet_text = snippet_text.replace("$pfts_2lqh>$", "").replace(
                            "$<pfts_2lqh$", ""
                        )
                        if len(snippet_text) > 200:
                            snippet_text = snippet_text[:200] + "..."
                        content_parts.append(f"{i + 1}. {snippet_text}")
                content_parts.append("")

            # Page name results
            if include_pages and result.get("pages"):
                pages = result["pages"]
                content_parts.append(f"## 📑 Matching Pages ({len(pages)} found)")
                for page in pages:
                    content_parts.append(f"- {page}")
                content_parts.append("")

            # File results
            if include_files and result.get("files"):
                files = result["files"]
                content_parts.append(f"## 📁 Matching Files ({len(files)} found)")
                for file_path in files:
                    content_parts.append(f"- {file_path}")
                content_parts.append("")

            # Pagination info
            if result.get("has-more?"):
                content_parts.append(
                    "📌 *More results available - increase limit to see more*"
                )

            # Summary
            total_results = (
                len(result.get("blocks", []))
                + len(result.get("pages", []))
                + len(result.get("files", []))
            )
            content_parts.append(f"\n**Total results found: {total_results}**")

            response_text = "\n".join(content_parts)

            return [TextContent(type="text", text=response_text)]

        except Exception as e:
            logger.error(f"Failed to search: {str(e)}")
            return [TextContent(
                type="text",
                text=f"❌ Search failed: {str(e)}"
            )]

class GetGraphInfoToolHandler(ToolHandler):
    def __init__(self):
        super().__init__("get_graph_info")

    def get_tool_description(self):
        return Tool(
            name=self.name,
            description="Get information about the current LogSeq graph, including its name, filesystem path, and user configuration.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        )

    def run_tool(self, args: dict) -> list[TextContent]:
        try:
            api = logseq.LogSeq(api_key=api_key)
            result = api.get_graph_info()

            graph = result.get("graph", {})
            configs = result.get("configs", {})

            parts = []
            parts.append(f"# Graph: {graph.get('name', 'Unknown')}")
            parts.append(f"Path: {graph.get('path', 'Unknown')}")
            parts.append(f"Format: {configs.get('preferredFormat', 'Unknown')}")
            parts.append(f"Date format: {configs.get('preferredDateFormat', 'Unknown')}")
            parts.append(f"Journals enabled: {configs.get('enabledJournals', 'Unknown')}")

            repos = configs.get("me", {}).get("repos", [])
            if repos:
                parts.append("\nAvailable graphs:")
                for repo in repos:
                    parts.append(f"  - {repo.get('url', 'Unknown')}")

            return [TextContent(type="text", text="\n".join(parts))]

        except Exception as e:
            logger.error(f"Failed to get graph info: {str(e)}")
            raise

class ReadPageFileToolHandler(ToolHandler):
    def __init__(self):
        super().__init__("read_page_file")

    def get_tool_description(self):
        return Tool(
            name=self.name,
            description="Read a page's raw markdown file directly from the LogSeq graph directory on disk. Useful as a fallback when the API doesn't return complete content.",
            inputSchema={
                "type": "object",
                "properties": {
                    "page_name": {
                        "type": "string",
                        "description": "Name of the page to read from disk"
                    }
                },
                "required": ["page_name"]
            }
        )

    def run_tool(self, args: dict) -> list[TextContent]:
        if "page_name" not in args:
            raise RuntimeError("page_name argument required")

        try:
            api = logseq.LogSeq(api_key=api_key)
            result = api.read_page_file(args["page_name"])

            parts = []
            parts.append(f"# {args['page_name']}")
            parts.append(f"Source: {result.get('source', 'unknown')}/{os.path.basename(result.get('path', ''))}")
            parts.append(f"Path: {result.get('path', 'Unknown')}")
            parts.append("")
            parts.append(result.get("content", ""))

            return [TextContent(type="text", text="\n".join(parts))]

        except FileNotFoundError as e:
            return [TextContent(type="text", text=f"❌ {str(e)}")]
        except Exception as e:
            logger.error(f"Failed to read page file: {str(e)}")
            raise
