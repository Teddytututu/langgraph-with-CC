"""Test SDK from within an asyncio background task (simulates uvicorn context)."""
import asyncio
import subprocess
import sys


async def do_sdk_call():
    from claude_agent_sdk import query, ClaudeAgentOptions
    options = ClaudeAgentOptions(
        allowed_tools=[],
        permission_mode="bypassPermissions",
        setting_sources=["user", "project"],
        system_prompt={"type": "preset", "preset": "claude_code", "append": "assistant"},
    )
    results = []
    async for msg in query(prompt="Reply only: OK", options=options):
        results.append(type(msg).__name__)
        if type(msg).__name__ == "ResultMessage":
            break
    print("Message types:", results)
    print("SDK in background task: PASSED")


async def http_handler_sim():
    """Simulates what uvicorn does: handle request, create background task."""
    # This is what api.py does:
    task = asyncio.create_task(do_sdk_call())
    # Wait for it (in reality uvicorn wouldn't, but we need the result here)
    await task


async def main():
    print("Testing SDK as asyncio background task...")
    try:
        await http_handler_sim()
    except Exception as e:
        print(f"FAILED: {e}")
        import traceback; traceback.print_exc()


# Use ProactorEventLoop on Windows (same as uvicorn --loop auto behavior)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

asyncio.run(main())
