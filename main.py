import os
from pathlib import Path
#from __future__ import annotations

import argparse
from lakera import LakeraAgent, LakeraAgentError
from claude import ClaudeAgent

DEFAULT_COOKIE_JAR = Path(os.getenv("USERDATA_DIR", "userdata")).expanduser() / "cookies.json"


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
        default=DEFAULT_COOKIE_JAR,
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
    #model = "claude-sonnet-4-5-20250929"
    model = "claude-haiku-4-5-20251001"
    max_rounds_level = 5
    claude = ClaudeAgent(model)
    try:
        with LakeraAgent(headless=False, cookie_jar=args.cookie_jar) as lakera:
            while True:
                level_description = lakera.describe_level()
                print("[lakera] Level description:\n" + level_description)

                claude.load_task_description_wipe(task_description=level_description)
                
                for i in range(max_rounds_level):
                    output = claude.model_turn()
                    answer, check = None, None
                    if output == "prompt":
                        prompt = claude.prompt
                        print("[claude] Submitting prompt to Lakera: " + prompt)
                        answer = lakera.submit_prompt(prompt)
                    elif output == "password":
                        password = claude.password
                        print("[claude] Submitting password to Lakera: " + password)
                        check = lakera.submit_password(password)

                    if answer:
                        print("[lakera] Produced answer: " + answer)
                    elif check:
                        print("[lakera] Password check result: " + check)

                    claude.process_lakera_output(answer, check)
                    if claude.success:
                        break
                print("Max rounds reached, exiting.")
                exit(0)
    except LakeraAgentError as exc:
        print(f"LakeraAgentError: {exc}")


if __name__ == "__main__":
    main()