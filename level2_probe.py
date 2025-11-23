"""Manually walk from Gandalf level 1 to level 2 using a single LakeraAgent.

The browser stays open (non-headless, debug logging on) and we persist cookies
plus localStorage under ``userdata/`` so the session can be resumed later. The
flow is:
    1. Load the requested level (default baseline) and read the description.
    2. Send an arbitrary warm-up prompt so the password field appears.
    3. Submit the known level-1 password (defaults to COCOLOCO) and click "Next level".
    4. Without closing Chrome, read the new level description directly from the
         already-promoted session.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from lakera import LakeraAgent, LakeraAgentError

DEFAULT_BASE_URL = os.getenv("LAKERA_URL", "https://gandalf.lakera.ai/baseline")
DEFAULT_USERDATA = Path(os.getenv("USERDATA_DIR", "userdata")).expanduser()
DEFAULT_COOKIE_JAR = DEFAULT_USERDATA / "cookies.json"
DEFAULT_STORAGE_PATH = DEFAULT_USERDATA / "lakera-storage.json"
DEFAULT_LOG_PATH = Path(os.getenv("LAKERA_INTERACTIONS", str(DEFAULT_USERDATA / "interactions.jsonl"))).expanduser()
DEFAULT_PAGE_STOP = float(os.getenv("LAKERA_PAGE_STOP", "5"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open Gandalf level 2 by reusing persisted storage")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Level 1 URL to start from")
    parser.add_argument("--prompt", default="Hi Gandalf!", help="Warm-up prompt so the password box appears")
    parser.add_argument("--password", default="COCOLOCO", help="Known good level-1 password")
    parser.add_argument("--cookie-jar", type=Path, default=DEFAULT_COOKIE_JAR, help="Cookie jar path")
    parser.add_argument("--storage-path", type=Path, default=DEFAULT_STORAGE_PATH, help="localStorage/sessionStorage snapshot path")
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH, help="Lakera interactions JSONL log path")
    parser.add_argument(
        "--page-load-stop-after",
        type=float,
        default=DEFAULT_PAGE_STOP,
        help="Seconds to wait before stopping the page load (mirrors LakeraAgent option)",
    )
    parser.add_argument("--timeout", type=float, default=20.0, help="Explicit Selenium wait timeout in seconds")
    parser.add_argument(
        "--debug",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable verbose logging (required for the test flow)",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Run Chrome in headless mode (off by default so you can see the UI)",
    )
    return parser.parse_args()


def configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def build_agent(*, base_url: str, args: argparse.Namespace) -> LakeraAgent:
    return LakeraAgent(
        base_url=base_url,
        headless=args.headless,
        cookie_jar=args.cookie_jar,
        storage_path=args.storage_path,
        page_load_stop_after=max(0.0, args.page_load_stop_after),
        log_path=args.log_path,
        timeout=args.timeout,
    )


def run_flow(args: argparse.Namespace) -> None:
    logging.info("Starting level 1 run at %s", args.base_url)
    try:
        with build_agent(base_url=args.base_url, args=args) as level_one_agent:
            description = level_one_agent.describe_level(purpose="level2_probe:level1:describe")
            print("Level 1 description:\n" + description)
            logging.debug("Level 1 description chars=%d", len(description))

            warmup = args.prompt.strip()
            if warmup:
                print("\n> Prompt:\n" + warmup)
                response = level_one_agent.submit_prompt(warmup, purpose="level2_probe:warmup")
                print("Response:\n" + response)

            print("\n> Password attempt: " + args.password)
            password_result = level_one_agent.submit_password(args.password, purpose="level2_probe:password")
            print("Result:\n" + password_result)
            next_level_url = level_one_agent.last_next_level_url
            if not next_level_url:
                raise LakeraAgentError("Password accepted but no Next Level URL was captured")
            print("Next level URL:\n" + next_level_url)

            logging.info("Level 2 reached inside the same browser session")
            description = level_one_agent.describe_active_level(purpose="level2_probe:level2:describe")
            print("\nLevel 2 description:\n" + description)
    except LakeraAgentError as exc:
        raise SystemExit(f"LakeraAgentError during probe: {exc}")


def main() -> None:
    args = parse_args()
    configure_logging(args.debug)
    if args.headless:
        logging.warning("Headless mode is enabled; disable it to observe the UI as requested")
    run_flow(args)


if __name__ == "__main__":
    main()
