"""Autonomous loop that drives Lakera Gandalf using an OpenRouter-hosted LLM."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from lakera import LakeraAgent, LakeraAgentError


LOG = logging.getLogger(__name__)

DEFAULT_USERDATA_DIR = Path(os.getenv("USERDATA_DIR", "userdata")).expanduser()
DEFAULT_TRANSCRIPT_DIR = DEFAULT_USERDATA_DIR / "transcripts"
DEFAULT_COOKIE_JAR = DEFAULT_USERDATA_DIR / "cookies.json"
DEFAULT_LATEST_URL = DEFAULT_USERDATA_DIR / "latest-level.url"
DEFAULT_STORAGE_PATH = DEFAULT_USERDATA_DIR / "lakera-storage.json"
DEFAULT_INTERACTIONS_LOG = Path(
    os.getenv("LAKERA_INTERACTIONS", str(DEFAULT_USERDATA_DIR / "interactions.jsonl"))
).expanduser()


@dataclass
class ParsedAction:
    tag: str
    content: str


class PromptRenderer:
    """Thin wrapper around Jinja2 for the main attack prompt."""

    def __init__(self, template_path: Path) -> None:
        if not template_path.exists():
            raise FileNotFoundError(f"Prompt template not found: {template_path}")
        self._env = Environment(
            loader=FileSystemLoader(str(template_path.parent)),
            autoescape=False,
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._template = self._env.get_template(template_path.name)

    def render(self, *, description: str, turns: List[Dict[str, str]], guidance: Optional[str]) -> str:
        return self._template.render(description=description, turns=turns, guidance=guidance)


class TranscriptLogger:
    """JSONL logger so each agent step can be replayed later."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def log(self, event: str, **payload: object) -> None:
        entry = {"timestamp": self._timestamp(), "event": event}
        entry.update(payload)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


class GandalfAutoAgent:
    """Coordinates Lakera, the prompt template, OpenRouter, and transcript logging."""

    TAG_PATTERN = re.compile(
        r"<(?P<tag>prompt|password)>(?P<body>.*?)</(?P=tag)>",
        re.IGNORECASE | re.DOTALL,
    )

    def __init__(
        self,
        *,
        template_path: Path,
        transcript_dir: Path,
        openrouter_model: Optional[str] = None,
        openrouter_api_key: Optional[str] = None,
        openrouter_referer: Optional[str] = None,
        openrouter_title: Optional[str] = None,
        http_timeout: float = 60.0,
        history_limit: int = 20,
        guidance: Optional[str] = None,
        lakera_kwargs: Optional[Dict[str, object]] = None,
        latest_url_path: Optional[Path] = None,
    ) -> None:
        self._renderer = PromptRenderer(template_path)
        self._guidance = guidance
        self._history_limit = max(0, history_limit)
        self._lakera_kwargs = lakera_kwargs or {}
        self._openrouter_model = openrouter_model or os.getenv("OPENROUTER_MODEL")
        self._openrouter_api_key = openrouter_api_key or os.getenv("OPENROUTER_API_KEY")
        if not self._openrouter_model:
            raise RuntimeError(
                "OpenRouter model is missing. Set OPENROUTER_MODEL or pass --openrouter-model explicitly."
            )
        self._http_timeout = http_timeout
        now = datetime.now(timezone.utc)
        run_id = now.strftime("%Y-%m-%dT%H-%M-%S")
        transcript_path = transcript_dir / f"transcript-{run_id}.jsonl"
        self._logger = TranscriptLogger(transcript_path)
        self._turns: List[Dict[str, str]] = []
        self._http = requests.Session()
        referer_header = openrouter_referer or os.getenv("OPENROUTER_REFERER")
        title_header = openrouter_title or os.getenv("OPENROUTER_TITLE")
        if referer_header:
            self._http.headers["HTTP-Referer"] = referer_header
        if title_header:
            self._http.headers["X-Title"] = title_header
        self._latest_url_path = latest_url_path

    def run(self, *, max_rounds: int = 10) -> None:
        LOG.info("Starting GandalfAutoAgent run; max_rounds=%d", max_rounds)
        with LakeraAgent(**self._lakera_kwargs) as lakera:
            level_number = 1
            LOG.debug("Fetching level %d description via Lakera", level_number)
            description = self._load_level_description(lakera, level_number, active=False)
            description_level = level_number
            LOG.debug("Level description length=%d", len(description))
            rounds_executed = 0
            for round_idx in range(1, max_rounds + 1):
                rounds_executed = round_idx
                if description_level != level_number:
                    LOG.debug(
                        "Description level (%d) lags behind active level (%d); refreshing",
                        description_level,
                        level_number,
                    )
                    description = self._load_level_description(lakera, level_number, active=True)
                    description_level = level_number
                LOG.info("Round %d/%d (level %d)", round_idx, max_rounds, level_number)
                llm_prompt = self._renderer.render(description=description, turns=self._turns, guidance=self._guidance)
                LOG.debug("Rendered template characters=%d", len(llm_prompt))
                print("\n===== OpenRouter Prompt (round", round_idx, ", level", level_number, ") =====")
                print(llm_prompt)
                print("===== End Prompt =====\n")
                self._logger.log("llm_prompt", round=round_idx, prompt=llm_prompt, level=level_number)
                llm_response = self._call_openrouter(llm_prompt)
                LOG.debug("OpenRouter response length=%d", len(llm_response))
                print("===== OpenRouter Response (round", round_idx, ", level", level_number, ") =====")
                print(llm_response)
                print("===== End Response =====\n")
                self._logger.log("llm_response", round=round_idx, response=llm_response, level=level_number)
                actions = self._extract_actions(llm_response)
                if not actions:
                    LOG.warning("Round %d produced no actionable XML tags", round_idx)
                    self._logger.log("no_actions", round=round_idx, response=llm_response, level=level_number)
                    break
                advanced_level = False
                for action in actions:
                    if action.tag == "prompt":
                        self._handle_prompt_action(lakera, action.content, round_idx, level_number)
                    elif action.tag == "password":
                        if self._handle_password_action(lakera, action.content, round_idx, level_number):
                            LOG.info("Password accepted for level %d", level_number)
                            level_number += 1
                            description = self._load_level_description(lakera, level_number, active=True)
                            description_level = level_number
                            self._turns.clear()
                            advanced_level = True
                            break
                if advanced_level:
                    continue
            LOG.info("Run complete; rounds=%d levels_reached=%d", rounds_executed, level_number)
            self._logger.log("run_complete", rounds=rounds_executed, level=level_number)

    def _call_openrouter(self, prompt: str) -> str:
        if not self._openrouter_api_key:
            raise RuntimeError("OpenRouter API key is missing. Set OPENROUTER_API_KEY or pass openrouter_api_key explicitly.")
        headers = {
            "Authorization": f"Bearer {self._openrouter_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._openrouter_model,
            "messages": [
                {"role": "user", "content": prompt},
            ],
        }
        LOG.debug("Calling OpenRouter model=%s", self._openrouter_model)
        response = self._http.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            data=json.dumps(payload),
            timeout=self._http_timeout,
        )
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("OpenRouter response did not include any choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not content:
            raise RuntimeError("OpenRouter response choice was missing content")
        return content

    def _extract_actions(self, llm_response: str) -> List[ParsedAction]:
        actions: List[ParsedAction] = []
        for match in self.TAG_PATTERN.finditer(llm_response):
            tag = match.group("tag").lower()
            body = match.group("body").strip()
            if body:
                actions.append(ParsedAction(tag=tag, content=body))
        if not actions:
            snippet = llm_response[:200].replace("\n", " ")
            LOG.debug("No XML tags found in response snippet=%r", snippet)
        return actions

    def _load_level_description(self, lakera: LakeraAgent, level_number: int, *, active: bool) -> str:
        purpose = f"auto_agent:level{level_number}:describe"
        if active:
            description = lakera.describe_active_level(purpose=purpose)
        else:
            description = lakera.describe_level(purpose=purpose)
        self._logger.log(
            "level_description",
            level=level_number,
            description=description,
            url=lakera.current_url,
        )
        return description

    def _handle_prompt_action(self, lakera: LakeraAgent, prompt: str, round_idx: int, level_number: int) -> None:
        LOG.info("Submitting prompt to Lakera (round %d, level %d)", round_idx, level_number)
        try:
            response = lakera.submit_prompt(prompt, purpose=f"auto_agent:round{round_idx}:prompt")
        except LakeraAgentError as exc:
            self._logger.log(
                "prompt_error",
                round=round_idx,
                level=level_number,
                prompt=prompt,
                error=str(exc),
            )
            self._append_turn("agent", prompt)
            self._append_turn("lakera", f"[error] prompt failed: {exc}")
            return
        if lakera.last_prompt_error:
            error_text = lakera.last_prompt_error
            tagged_response = f"[validation-error] {error_text}"
            self._logger.log(
                "prompt_validation_error",
                round=round_idx,
                level=level_number,
                prompt=prompt,
                error=error_text,
            )
            self._append_turn("agent", prompt)
            self._append_turn("lakera", tagged_response)
            return
        self._logger.log(
            "prompt_submission",
            round=round_idx,
            level=level_number,
            prompt=prompt,
            response=response,
        )
        self._append_turn("agent", prompt)
        self._append_turn("lakera", response)

    def _handle_password_action(self, lakera: LakeraAgent, password: str, round_idx: int, level_number: int) -> bool:
        LOG.info("Submitting password attempt to Lakera (round %d, level %d)", round_idx, level_number)
        try:
            response = lakera.submit_password(password, purpose=f"auto_agent:round{round_idx}:password")
        except LakeraAgentError as exc:
            self._logger.log(
                "password_error",
                round=round_idx,
                password=password,
                level=level_number,
                error=str(exc),
            )
            self._append_turn("agent", f"[password attempt] {password}")
            self._append_turn("lakera", f"[error] password submission failed: {exc}")
            return False
        self._logger.log(
            "password_submission",
            round=round_idx,
            password=password,
            response=response,
            level=level_number,
            next_level=lakera.last_next_level_url,
        )
        self._append_turn("agent", f"[password attempt] {password}")
        self._append_turn("lakera", response)
        if lakera.last_next_level_url:
            self._logger.log(
                "next_level",
                level=level_number + 1,
                url=lakera.last_next_level_url,
            )
            self._persist_latest_url(lakera.last_next_level_url)
            return True
        return False

    def _append_turn(self, role: str, content: str) -> None:
        self._turns.append({"role": role, "content": content})
        if self._history_limit and len(self._turns) > self._history_limit:
            self._turns = self._turns[-self._history_limit :]

    def _persist_latest_url(self, url: str) -> None:
        if not self._latest_url_path:
            return
        try:
            self._latest_url_path.parent.mkdir(parents=True, exist_ok=True)
            self._latest_url_path.write_text(url, encoding="utf-8")
            LOG.info("Saved next level URL to %s", self._latest_url_path)
        except OSError as exc:
            LOG.warning("Failed to write latest level URL: %s", exc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Autonomous Lakera Gandalf agent")
    parser.add_argument("--cookie-jar", type=Path, default=DEFAULT_COOKIE_JAR, help="Path to persist Lakera cookies")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True, help="Run Chrome in headless mode")
    parser.add_argument("--template", type=Path, default=Path("prompts/main.txt"), help="Path to the Jinja prompt template")
    parser.add_argument(
        "--transcript-dir",
        type=Path,
        default=DEFAULT_TRANSCRIPT_DIR,
        help="Directory where transcript-[date].jsonl is stored",
    )
    parser.add_argument(
        "--lakera-url",
        default=os.getenv("LAKERA_URL", "https://gandalf.lakera.ai/baseline"),
        help="Lakera Gandalf level URL to target (defaults to LAKERA_URL or baseline)",
    )
    parser.add_argument(
        "--page-load-stop-after",
        type=float,
        default=float(os.getenv("LAKERA_PAGE_STOP", "5")),
        help="Seconds to wait before forcibly stopping page load (0 disables)",
    )
    latest_url_default = Path(os.getenv("LAKERA_LATEST_URL", str(DEFAULT_LATEST_URL)))
    parser.add_argument(
        "--latest-url-path",
        type=Path,
        default=latest_url_default,
        help="File path where the newest level URL should be written",
    )
    storage_default = Path(os.getenv("LAKERA_STORAGE", str(DEFAULT_STORAGE_PATH)))
    parser.add_argument(
        "--storage-path",
        type=Path,
        default=storage_default,
        help="Path for persisting Lakera local/session storage",
    )
    parser.add_argument(
        "--openrouter-model",
        default=os.getenv("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet"),
        help="OpenRouter model identifier (defaults to OPENROUTER_MODEL env var)",
    )
    parser.add_argument("--openrouter-key", help="OpenRouter API key (falls back to OPENROUTER_API_KEY env var)")
    parser.add_argument(
        "--openrouter-referer",
        default=os.getenv("OPENROUTER_REFERER"),
        help="HTTP Referer header required by OpenRouter",
    )
    parser.add_argument(
        "--openrouter-title",
        default=os.getenv("OPENROUTER_TITLE"),
        help="Title header identifying this integration",
    )
    parser.add_argument("--guidance", help="Optional operator guidance injected into the template")
    parser.add_argument("--history-limit", type=int, default=20, help="Max number of turns to feed back into the template")
    parser.add_argument("--max-rounds", type=int, default=10, help="Maximum LLM-Lakera cycles to execute")
    parser.add_argument("--http-timeout", type=float, default=60.0, help="Timeout for OpenRouter requests in seconds")
    parser.add_argument("--debug", action="store_true", help="Enable verbose console logging for troubleshooting")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    lakera_kwargs = {
        "cookie_jar": args.cookie_jar,
        "headless": args.headless,
        "base_url": args.lakera_url,
        "page_load_stop_after": max(0.0, args.page_load_stop_after),
        "storage_path": args.storage_path,
        "log_path": DEFAULT_INTERACTIONS_LOG,
    }
    agent = GandalfAutoAgent(
        template_path=args.template,
        transcript_dir=args.transcript_dir,
        openrouter_model=args.openrouter_model,
        openrouter_api_key=args.openrouter_key,
        openrouter_referer=args.openrouter_referer,
        openrouter_title=args.openrouter_title,
        guidance=args.guidance,
        history_limit=args.history_limit,
        http_timeout=args.http_timeout,
        lakera_kwargs=lakera_kwargs,
        latest_url_path=args.latest_url_path,
    )
    try:
        agent.run(max_rounds=args.max_rounds)
    except LakeraAgentError as exc:
        agent._logger.log("lakera_error", error=str(exc))
        raise


if __name__ == "__main__":
    main()
