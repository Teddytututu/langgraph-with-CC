"""Quick SDK smoke test."""
import asyncio
import os

async def main():
    print("ANTHROPIC_API_KEY:", bool(os.environ.get("ANTHROPIC_API_KEY")))
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        print("SDK imported OK")
        options = ClaudeAgentOptions(
            allowed_tools=[],
            permission_mode="bypassPermissions",
            setting_sources=["user", "project"],
            system_prompt={"type": "preset", "preset": "claude_code", "append": "你是助手"},
        )
        count = 0
        async for msg in query(prompt="reply just: OK", options=options):
            print(f"msg {count}: {type(msg).__name__}", str(msg)[:120])
            count += 1
            if count >= 5:
                break
        print("SDK test PASSED")
    except Exception as e:
        print(f"SDK test FAILED: {e}")
        import traceback; traceback.print_exc()

asyncio.run(main())
