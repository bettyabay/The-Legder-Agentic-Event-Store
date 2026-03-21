import asyncio
from src.llm.client import create_async_anthropic_client

client = create_async_anthropic_client()


async def main() -> None:
    resp = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=20,
        system="You are a test agent.",
        messages=[{"role": "user", "content": "Say OK."}],
    )
    print(resp.content[0].text)
    print(resp.usage)


if __name__ == "__main__":
    asyncio.run(main())