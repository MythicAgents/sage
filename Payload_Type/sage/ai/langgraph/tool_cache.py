"""
Generic tool cache manager for storing and retrieving tool call results.

This module provides a flexible caching system that can store results from any tool call,
with support for TTL (time-to-live), cache invalidation, and async operations.
"""

import json
import time
import hashlib
import aiosqlite
from typing import Any, Optional
from mythic_container.logging import logger

# Import logging fix - handle both relative and absolute imports
try:
    from .logging_fix import ensure_logger_initialized, force_flush_all_handlers
except ImportError:
    from logging_fix import ensure_logger_initialized, force_flush_all_handlers


class ToolCache:
    """A generic cache manager for tool call results stored in SQLite.

    This cache can store results from any tool call using a composite key of:
    - tool_name: The name of the tool (e.g., "get_all_commands_for_payloadtype")
    - cache_key: A string representation of the parameters (e.g., "sage" or JSON of params)

    Features:
    - TTL (time-to-live) support for automatic expiration
    - Async-safe operations
    - JSON serialization for complex data structures
    - Cache statistics and management
    """

    def __init__(self, db_path: str = "sage.db"):
        """Initialize the ToolCache with a database path.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = db_path
        self._initialized = False

    async def initialize(self):
        """Initialize the cache database and create tables if needed."""
        # Ensure logger is working before any log calls
        ensure_logger_initialized()

        if self._initialized:
            logger.debug("ToolCache already initialized, skipping")
            force_flush_all_handlers()
            return

        logger.info(f"🗄️  Initializing ToolCache with database: {self.db_path}")
        force_flush_all_handlers()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tool_cache (
                    tool_name TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    ttl_seconds INTEGER,
                    PRIMARY KEY (tool_name, cache_key)
                )
            """)

            # Create index for timestamp-based queries
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_tool_cache_timestamp
                ON tool_cache(timestamp)
            """)

            await db.commit()

        self._initialized = True
        logger.info("✅ ToolCache initialized successfully")
        force_flush_all_handlers()

    @staticmethod
    def _generate_cache_key(params: Any) -> str:
        """Generate a cache key from parameters.

        Args:
            params: Can be a string, dict, list, or any JSON-serializable object

        Returns:
            A string cache key
        """
        if isinstance(params, str):
            return params

        # For complex objects, create a deterministic JSON string
        # Sort keys to ensure consistent ordering
        try:
            json_str = json.dumps(params, sort_keys=True)
            # Use hash for very long parameter strings
            if len(json_str) > 255:
                return hashlib.sha256(json_str.encode()).hexdigest()
            return json_str
        except (TypeError, ValueError) as e:
            logger.warning(f"Failed to serialize params to cache key: {e}")
            return str(params)

    async def get(self, tool_name: str, params: Any = None) -> Optional[Any]:
        """Retrieve a cached result for a tool call.

        Args:
            tool_name: Name of the tool (e.g., "get_all_commands_for_payloadtype")
            params: Parameters used for the tool call (can be None for parameterless tools)

        Returns:
            The cached result if found and not expired, None otherwise
        """
        if not self._initialized:
            logger.warning("ToolCache not initialized, initializing now...")
            await self.initialize()

        cache_key = self._generate_cache_key(params) if params is not None else ""
        logger.debug(f"Cache lookup: tool='{tool_name}', key='{cache_key}'")

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT result_json, timestamp, ttl_seconds FROM tool_cache WHERE tool_name = ? AND cache_key = ?",
                (tool_name, cache_key)
            ) as cursor:
                row = await cursor.fetchone()

                if not row:
                    logger.debug(f"Cache miss: {tool_name} with key '{cache_key}'")
                    return None

                result_json, timestamp, ttl_seconds = row

                # Check if expired
                if ttl_seconds is not None:
                    age = time.time() - timestamp
                    if age > ttl_seconds:
                        logger.debug(f"Cache expired: {tool_name} with key '{cache_key}' (age: {age}s, ttl: {ttl_seconds}s)")
                        # Delete expired entry
                        await db.execute(
                            "DELETE FROM tool_cache WHERE tool_name = ? AND cache_key = ?",
                            (tool_name, cache_key)
                        )
                        await db.commit()
                        return None

                # Parse and return result
                try:
                    result = json.loads(result_json)
                    logger.debug(f"Cache hit: {tool_name} with key '{cache_key}'")
                    return result
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse cached result: {e}")
                    return None

    async def set(self, tool_name: str, params: Any, result: Any, ttl_seconds: Optional[int] = None):
        """Store a tool call result in the cache.

        Args:
            tool_name: Name of the tool
            params: Parameters used for the tool call (can be None)
            result: The result to cache (must be JSON-serializable)
            ttl_seconds: Optional time-to-live in seconds (None = no expiration)
        """
        if not self._initialized:
            logger.warning("ToolCache not initialized when trying to set, initializing now...")
            await self.initialize()

        cache_key = self._generate_cache_key(params) if params is not None else ""
        logger.debug(f"Cache set: tool='{tool_name}', key='{cache_key}', ttl={ttl_seconds}s")

        try:
            result_json = json.dumps(result)
        except (TypeError, ValueError) as e:
            logger.error(f"Failed to serialize result for caching: {e}")
            return

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO tool_cache (tool_name, cache_key, result_json, timestamp, ttl_seconds)
                VALUES (?, ?, ?, ?, ?)
            """, (tool_name, cache_key, result_json, time.time(), ttl_seconds))

            await db.commit()

        logger.debug(f"Cached result for {tool_name} with key '{cache_key}' (ttl: {ttl_seconds}s)")

    async def invalidate(self, tool_name: str, params: Any = None):
        """Invalidate a specific cache entry.

        Args:
            tool_name: Name of the tool
            params: Parameters to match (if None, invalidates ALL entries for this tool)
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            if params is None:
                # Invalidate all entries for this tool
                await db.execute("DELETE FROM tool_cache WHERE tool_name = ?", (tool_name,))
                logger.info(f"Invalidated all cache entries for {tool_name}")
            else:
                cache_key = self._generate_cache_key(params)
                await db.execute(
                    "DELETE FROM tool_cache WHERE tool_name = ? AND cache_key = ?",
                    (tool_name, cache_key)
                )
                logger.info(f"Invalidated cache entry for {tool_name} with key '{cache_key}'")

            await db.commit()

    async def clear_all(self):
        """Clear all cache entries."""
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM tool_cache")
            await db.commit()

        logger.info("Cleared all cache entries")

    async def cleanup_expired(self):
        """Remove all expired cache entries."""
        if not self._initialized:
            await self.initialize()

        current_time = time.time()

        async with aiosqlite.connect(self.db_path) as db:
            # Delete entries where ttl_seconds is not NULL and entry is expired
            result = await db.execute("""
                DELETE FROM tool_cache
                WHERE ttl_seconds IS NOT NULL
                AND (timestamp + ttl_seconds) < ?
            """, (current_time,))

            await db.commit()

            deleted_count = result.rowcount
            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} expired cache entries")

    async def get_stats(self) -> dict:
        """Get cache statistics.

        Returns:
            Dictionary with cache statistics
        """
        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            # Total entries
            async with db.execute("SELECT COUNT(*) FROM tool_cache") as cursor:
                total = (await cursor.fetchone())[0]

            # Entries by tool
            async with db.execute("""
                SELECT tool_name, COUNT(*) as count
                FROM tool_cache
                GROUP BY tool_name
            """) as cursor:
                by_tool = {row[0]: row[1] async for row in cursor}

            # Expired entries
            current_time = time.time()
            async with db.execute("""
                SELECT COUNT(*) FROM tool_cache
                WHERE ttl_seconds IS NOT NULL
                AND (timestamp + ttl_seconds) < ?
            """, (current_time,)) as cursor:
                expired = (await cursor.fetchone())[0]

        return {
            "total_entries": total,
            "by_tool": by_tool,
            "expired_entries": expired
        }
