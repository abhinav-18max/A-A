"""Report which WebMCP tools a live site exposes, with and without Chromium's WebMCP flag.

    uv run python tools/webmcp_survey.py https://www.example.com [...]

"available" without the flag means the site is in Chrome's origin trial, so tools also work
for ordinary Chrome users. Tools only with the flag need SessionConfig(webmcp=True). Tools
often register per page, so survey the page an agent would actually use.
"""

import argparse
import asyncio
import tempfile

from mba import Adapter, SessionConfig


async def survey(url, flag, settle_s):
    config = SessionConfig(recording=False, webmcp=flag, target_url=url, startup_timeout_s=90)
    with tempfile.TemporaryDirectory() as root:
        async with Adapter(config, artifact_dir=root) as session:
            await asyncio.sleep(settle_s)
            listing = await session.webmcp.tools()
            registered = sum(
                1
                for e in session.core.evidence.read(0)
                if e.type == "webmcp.tools_changed" and e.data["change"] == "added"
            )
            return listing, registered


async def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--settle", type=float, default=6, help="seconds to wait after load")
    args = parser.parse_args()
    for url in args.urls:
        for flag in (False, True):
            try:
                listing, registered = await survey(url, flag, args.settle)
            except Exception as exc:
                print(f"{url}  flag={'on ' if flag else 'off'}  error: {exc}")
                continue
            names = sorted(t["name"] for t in listing["tools"])
            print(
                f"{url}  flag={'on ' if flag else 'off'}  api={'yes' if listing['available'] else 'no '}"
                f"  tools={len(names)}  journaled={registered}"
                f"  blocked_frames={len(listing['blocked_frames'])}  {', '.join(names[:8])}"
                + (" …" if len(names) > 8 else "")
            )


if __name__ == "__main__":
    asyncio.run(main())
