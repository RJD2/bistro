"""Keep the original script command available alongside `python -m bistro`"""

import asyncio

from bistro.bot import main


if __name__ == "__main__":
    asyncio.run(main())
