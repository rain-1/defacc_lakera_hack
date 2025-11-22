"""Submit a known-wrong password to observe Gandalf's response."""

from __future__ import annotations

import argparse
from pathlib import Path

from lakera import LakeraAgent, LakeraAgentError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Try submitting a password with LakeraAgent")
    parser.add_argument(
        "--password",
        default="wrong-password-123",
        help="Password value to submit.",
    )
    parser.add_argument(
        "--cookie-jar",
        type=Path,
        default=Path("cookies.json"),
        help="Cookie jar path for persisting session state.",
    )
    parser.add_argument(
        "--warmup-prompt",
        default="Hello Gandalf!",
        help="Prompt to send first so the password box appears.",
    )
    parser.add_argument(
        "--purpose",
        default="password_probe",
        help="Label recorded in the JSONL log for these interactions.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        with LakeraAgent(cookie_jar=args.cookie_jar) as agent:
            print("Level description:\n" + agent.describe_level(purpose=f"{args.purpose}:describe"))
            warmup = args.warmup_prompt.strip()
            if warmup:
                print("\n> Warm-up prompt:\n" + warmup)
                warm_response = agent.submit_prompt(warmup, purpose=f"{args.purpose}:warmup")
                print("Response:\n" + warm_response)
            print("\n> Password attempt:")
            result = agent.submit_password(args.password, purpose=f"{args.purpose}:password")
            print("Result:\n" + result)
            if agent.last_next_level_url:
                print("Next level URL:\n" + agent.last_next_level_url)
    except LakeraAgentError as exc:
        print(f"LakeraAgentError: {exc}")


if __name__ == "__main__":
    main()
