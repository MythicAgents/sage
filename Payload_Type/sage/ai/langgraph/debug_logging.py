#!/usr/bin/env python3
"""
Logging diagnostic script to identify why mythic_container.logging stops working.

This script tests various logging configurations and provides workarounds.
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from mythic_container.logging import logger as mythic_logger


def test_standard_logging():
    """Test if standard Python logging works."""
    print("\n" + "="*60)
    print("TEST 1: Standard Python Logging")
    print("="*60)

    # Configure standard logging
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(levelname)s %(asctime)s %(funcName)s %(lineno)d : %(message)s',
        stream=sys.stdout,
        force=True  # Force reconfiguration
    )

    std_logger = logging.getLogger(__name__)

    print("Testing standard logger:")
    std_logger.debug("✅ DEBUG level works")
    std_logger.info("✅ INFO level works")
    std_logger.warning("✅ WARNING level works")
    std_logger.error("✅ ERROR level works")

    sys.stdout.flush()


def test_mythic_logging():
    """Test if mythic_container.logging works."""
    print("\n" + "="*60)
    print("TEST 2: Mythic Container Logging")
    print("="*60)

    print(f"Mythic logger type: {type(mythic_logger)}")
    print(f"Mythic logger level: {mythic_logger.level}")
    print(f"Mythic logger handlers: {mythic_logger.handlers}")

    if mythic_logger.handlers:
        for i, handler in enumerate(mythic_logger.handlers):
            print(f"  Handler {i}: {type(handler).__name__}")
            print(f"    Level: {handler.level}")
            print(f"    Formatter: {handler.formatter}")

    print("\nTesting mythic logger:")
    mythic_logger.debug("✅ DEBUG level works")
    mythic_logger.info("✅ INFO level works")
    mythic_logger.warning("✅ WARNING level works")
    mythic_logger.error("✅ ERROR level works")

    sys.stdout.flush()


def test_logging_after_imports():
    """Test logging after importing model dependencies."""
    print("\n" + "="*60)
    print("TEST 3: Logging After Heavy Imports")
    print("="*60)

    try:
        print("Importing tool_cache...")
        from tool_cache import ToolCache

        print("Importing model...")
        # Don't actually import Model to avoid initialization, just check if logger still works

        print("Testing mythic logger after imports:")
        mythic_logger.info("✅ Logger still works after imports")

        sys.stdout.flush()

    except Exception as e:
        print(f"❌ Import failed: {e}")
        import traceback
        traceback.print_exc()


async def test_async_logging():
    """Test logging in async context."""
    print("\n" + "="*60)
    print("TEST 4: Async Logging")
    print("="*60)

    print("Testing in async function:")
    mythic_logger.info("✅ Logger works in async context")

    # Simulate some async work
    await asyncio.sleep(0.1)

    mythic_logger.info("✅ Logger works after await")

    sys.stdout.flush()


def test_forced_stdout():
    """Test direct stdout writing as fallback."""
    print("\n" + "="*60)
    print("TEST 5: Direct stdout (Fallback)")
    print("="*60)

    sys.stdout.write("✅ Direct stdout write works\n")
    sys.stdout.flush()

    print("✅ Print function works")
    sys.stdout.flush()


async def test_with_tool_cache():
    """Test logging with actual ToolCache initialization."""
    print("\n" + "="*60)
    print("TEST 6: Logging with ToolCache")
    print("="*60)

    try:
        from tool_cache import ToolCache

        mythic_logger.info("🗄️  About to initialize ToolCache...")
        sys.stdout.flush()

        cache = ToolCache("test_debug.db")

        mythic_logger.info("📋 Calling cache.initialize()...")
        sys.stdout.flush()

        await cache.initialize()

        mythic_logger.info("✅ ToolCache initialized, logger still working!")
        sys.stdout.flush()

        # Try a cache operation
        mythic_logger.info("💾 Testing cache.set()...")
        sys.stdout.flush()

        await cache.set("test_tool", "test_param", {"data": "test"}, ttl_seconds=60)

        mythic_logger.info("✅ cache.set() completed, logger still working!")
        sys.stdout.flush()

        # Clean up
        import os
        if os.path.exists("test_debug.db"):
            os.remove("test_debug.db")

    except Exception as e:
        print(f"❌ ToolCache test failed: {e}")
        import traceback
        traceback.print_exc()


def create_fallback_logger():
    """Create a fallback logger that definitely works."""
    print("\n" + "="*60)
    print("CREATING FALLBACK LOGGER")
    print("="*60)

    fallback = logging.getLogger("sage_fallback")
    fallback.setLevel(logging.DEBUG)

    # Force stdout handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        '%(levelname)s %(asctime)s %(funcName)s %(lineno)d : %(message)s'
    )
    handler.setFormatter(formatter)

    fallback.addHandler(handler)
    fallback.propagate = False

    print("Testing fallback logger:")
    fallback.info("✅ Fallback logger works")
    sys.stdout.flush()

    return fallback


async def main():
    """Run all diagnostic tests."""
    print("\n" + "="*60)
    print("SAGE LOGGING DIAGNOSTIC")
    print("="*60)

    # Test 1: Standard logging
    test_standard_logging()

    # Test 2: Mythic logging
    test_mythic_logging()

    # Test 3: After imports
    test_logging_after_imports()

    # Test 4: Async context
    await test_async_logging()

    # Test 5: Direct stdout
    test_forced_stdout()

    # Test 6: With ToolCache
    await test_with_tool_cache()

    # Create fallback
    fallback = create_fallback_logger()

    print("\n" + "="*60)
    print("DIAGNOSTIC COMPLETE")
    print("="*60)
    print("\nIf mythic_container.logging fails but fallback works,")
    print("we can patch model.py to use the fallback logger.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
