"""Selenium-backed helper for interacting with https://gandalf.lakera.ai."""

from __future__ import annotations

import json
import os
from pathlib import Path
from shutil import which
from typing import Iterable, Optional
from datetime import datetime
import time

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


class LakeraAgentError(RuntimeError):
    """General failure interacting with the Lakera page."""


DEFAULT_USERDATA_DIR = Path(os.getenv("USERDATA_DIR", "userdata")).expanduser()
DEFAULT_LOG_PATH = Path(
    os.getenv("LAKERA_INTERACTIONS", str(DEFAULT_USERDATA_DIR / "interactions.jsonl"))
).expanduser()
EMPTY_ANSWER_GRACE_SECONDS = float(os.getenv("LAKERA_EMPTY_ANSWER_GRACE", "2"))
EMPTY_ANSWER_POLL_SECONDS = float(os.getenv("LAKERA_EMPTY_ANSWER_POLL", "0.2"))


class LakeraAgent:
    """Wraps a Selenium session to fetch descriptions, prompts, and password guesses."""

    def __init__(
        self,
        *,
        base_url: str = "https://gandalf.lakera.ai/baseline",
        cookie_jar: Optional[Path] = None,
        headless: bool = True,
        timeout: float = 15.0,
        chrome_binary: Optional[Path] = None,
        log_path: Optional[Path] = None,
        storage_path: Optional[Path] = None,
        page_load_stop_after: float = 5.0,
    ) -> None:
        self._base_url = base_url
        self._cookie_jar = cookie_jar
        self._timeout = timeout
        self._headless = headless
        self._chrome_binary = chrome_binary
        self._log_path = Path(log_path) if log_path else DEFAULT_LOG_PATH
        self._storage_path = storage_path
        self._page_load_stop_after = max(0.0, page_load_stop_after)
        self._last_next_level_url: Optional[str] = None
        self._last_prompt_error: Optional[str] = None
        self._driver = self._build_driver()
        self._wait = WebDriverWait(self._driver, self._timeout)

    @staticmethod
    def _sanitize_sendable_text(text: str) -> tuple[str, bool]:
        """Remove characters outside the Basic Multilingual Plane (ChromeDriver limitation)."""
        sanitized = "".join(ch for ch in text if ord(ch) <= 0xFFFF)
        return sanitized, sanitized != text

    def _build_driver(self) -> webdriver.Chrome:
        binary_path = self._resolve_browser_binary()
        options = Options()
        if self._headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        if binary_path:
            options.binary_location = binary_path

        try:
            driver = webdriver.Chrome(options=options)
        except WebDriverException as exc:
            raise LakeraAgentError("failed to start Chrome WebDriver") from exc

        self._navigate_to(self._base_url, driver=driver)
        needs_refresh = False
        if self._storage_path and self._storage_path.exists():
            if self._restore_storage(driver):
                needs_refresh = True
        if self._cookie_jar and self._cookie_jar.exists():
            self._load_cookies(driver)
            needs_refresh = True
        if needs_refresh:
            driver.refresh()
        return driver

    def _load_cookies(self, driver: webdriver.Chrome) -> None:
        if not self._cookie_jar or not self._cookie_jar.exists():
            return

        try:
            cookies = json.loads(self._cookie_jar.read_text())
        except (json.JSONDecodeError, OSError):
            return

        for cookie in cookies:
            if "expiry" in cookie:
                cookie["expiry"] = int(cookie["expiry"])
            try:
                driver.add_cookie(cookie)
            except WebDriverException:
                continue

    def _restore_storage(self, driver: webdriver.Chrome) -> bool:
        if not self._storage_path or not self._storage_path.exists():
            return False
        try:
            snapshot = json.loads(self._storage_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        local_entries = snapshot.get("local") or {}
        session_entries = snapshot.get("session") or {}
        script = """
            (function(ls, ss) {
                if (ls && typeof ls === 'object') {
                    Object.keys(ls).forEach(key => {
                        try { localStorage.setItem(key, ls[key]); } catch (_) {}
                    });
                }
                if (ss && typeof ss === 'object') {
                    Object.keys(ss).forEach(key => {
                        try { sessionStorage.setItem(key, ss[key]); } catch (_) {}
                    });
                }
            })(arguments[0], arguments[1]);
        """
        try:
            driver.execute_script(script, local_entries, session_entries)
        except WebDriverException:
            return False
        return bool(local_entries or session_entries)

    def _capture_storage(self) -> Optional[dict]:
        script = """
            const result = { local: {}, session: {} };
            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                result.local[key] = localStorage.getItem(key);
            }
            for (let i = 0; i < sessionStorage.length; i++) {
                const key = sessionStorage.key(i);
                result.session[key] = sessionStorage.getItem(key);
            }
            return result;
        """
        try:
            snapshot = self._driver.execute_script(script)
        except WebDriverException:
            return None
        return snapshot

    def save_cookies(self) -> None:
        if not self._cookie_jar:
            return
        cookies = self._driver.get_cookies()
        self._cookie_jar.parent.mkdir(parents=True, exist_ok=True)
        self._cookie_jar.write_text(json.dumps(cookies), encoding="utf-8")

    def save_storage(self) -> None:
        if not self._storage_path:
            return
        snapshot = self._capture_storage()
        if snapshot is None:
            return
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            self._storage_path.write_text(json.dumps(snapshot), encoding="utf-8")
        except OSError:
            pass

    def close(self, *, save_state: bool = True) -> None:
        if save_state:
            self.save_cookies()
            self.save_storage()
        self._driver.quit()

    def __enter__(self) -> "LakeraAgent":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _find_answer_text(self) -> Optional[str]:
        answers = self._driver.find_elements(By.CSS_SELECTOR, "p.answer")
        if not answers:
            return None
        return answers[-1].text.strip()

    def _find_prompt_error(self) -> Optional[str]:
        selectors = "p.text-red-500, p[class*='text-red']"
        try:
            error = self._driver.find_element(By.CSS_SELECTOR, selectors)
        except NoSuchElementException:
            return None
        text = error.text.strip()
        return text or None

    def _log_event(
        self,
        action: str,
        request: dict,
        *,
        response: Optional[str] = None,
        error: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> None:
        if not self._log_path:
            return
        entry = {
            "timestamp": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
            "action": action,
            "purpose": request.get("purpose"),
            "request": request,
            "response": response,
            "error": error,
            "status": "error" if error else "success",
        }
        if extra:
            entry.update(extra)
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    def _get_password_alert_text(self) -> Optional[str]:
        alerts = self._driver.find_elements(By.CSS_SELECTOR, "div.customAlert")
        if not alerts:
            return None
        # The newest alert is appended at the end of the list.
        for alert in reversed(alerts):
            text = alert.text.strip()
            if text:
                return text
        return None

    def _wait_for_password_alert(self) -> str:
        def _alert_ready(_: webdriver.Chrome) -> Optional[str]:
            return self._get_password_alert_text()

        try:
            result = self._wait.until(_alert_ready)
        except TimeoutException as exc:
            raise LakeraAgentError("timed out waiting for password result") from exc
        return result or ""

    def _dismiss_password_alerts(self) -> None:
        try:
            self._driver.execute_script("document.querySelectorAll('.customAlert').forEach(el => el.remove());")
        except WebDriverException:
            pass

    def _resolve_browser_binary(self) -> str:
        if self._chrome_binary:
            binary_path = str(self._chrome_binary.expanduser())
            return binary_path

        candidates = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
        for binary in candidates:
            path = which(binary)
            if path:
                return path

        raise LakeraAgentError("could not find a Chrome/Chromium binary in PATH")

    def _submit_form(self, element: WebElement) -> None:
        form = element.find_element(By.XPATH, "ancestor::form")
        try:
            submit = form.find_element(By.CSS_SELECTOR, "button[type='submit']")
        except NoSuchElementException:
            form.submit()
            return
        try:
            self._driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", submit)
        except WebDriverException:
            pass
        try:
            submit.click()
        except ElementClickInterceptedException:
            try:
                self._driver.execute_script("arguments[0].click();", submit)
            except WebDriverException:
                form.submit()
        except WebDriverException:
            form.submit()

    def _wait_for_prompt_result(self, previous: Optional[str] = None) -> tuple[str, str]:
        def _result_ready(driver: webdriver.Chrome) -> Optional[tuple[str, str]]:
            error_text = self._find_prompt_error()
            if error_text:
                return ("error", error_text)
            text = self._find_answer_text()
            if text is None:
                return None if previous else ("answer", "")
            if not previous or text != previous:
                return ("answer", text)
            return False

        try:
            result = self._wait.until(_result_ready)
        except TimeoutException as exc:
            raise LakeraAgentError("timed out waiting for prompt result") from exc
        if not result:
            return ("answer", "")
        return result

    def _capture_next_level_url(self) -> Optional[str]:
        try:
            alert = self._driver.find_element(By.CSS_SELECTOR, "div.customAlert")
        except NoSuchElementException:
            return None
        candidates = alert.find_elements(By.TAG_NAME, "button")
        target_button = None
        for button in candidates:
            text = button.text.strip().lower()
            if "next level" in text:
                target_button = button
                break
        if not target_button:
            return None

        original_url = self._driver.current_url
        try:
            target_button.click()
        except WebDriverException:
            return None

        try:
            self._wait.until(lambda drv: drv.current_url != original_url)
        except TimeoutException:
            return None
        return self._driver.current_url

    def describe_level(self, purpose: Optional[str] = None) -> str:
        payload = {"purpose": purpose or "describe_level", "url": self._base_url}
        self._navigate_to(self._base_url)
        return self._fetch_level_description("describe_level", payload)

    def describe_active_level(self, purpose: Optional[str] = None) -> str:
        payload = {
            "purpose": purpose or "describe_active_level",
            "url": self._driver.current_url if self._driver else None,
        }
        return self._fetch_level_description("describe_active_level", payload)

    def _fetch_level_description(self, action: str, payload: dict) -> str:
        for attempt in (1, 2):
            try:
                self._prepare_level_page()
                description = self._wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "p.description"))
                )
                text = description.text.strip()
                self._log_event(action, payload, response=text)
                return text
            except TimeoutException as exc:
                if attempt == 2:
                    error_message = "could not load level description"
                    self._log_event(action, payload, error=error_message)
                    raise LakeraAgentError(error_message) from exc
                try:
                    self._driver.refresh()
                except WebDriverException:
                    pass
        raise LakeraAgentError("unreachable level description fetch state")

    def _navigate_to(self, url: str, *, driver: Optional[webdriver.Chrome] = None) -> None:
        target = driver or self._driver
        if not target:
            raise LakeraAgentError("WebDriver not available for navigation")
        target.get(url)
        if not self._page_load_stop_after:
            return
        try:
            WebDriverWait(target, self._page_load_stop_after).until(
                lambda drv: drv.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException:
            self._stop_page_load(driver=target)

    def _stop_page_load(self, *, driver: Optional[webdriver.Chrome] = None) -> None:
        target = driver or self._driver
        if not target:
            return
        try:
            target.execute_script("window.stop();")
        except WebDriverException:
            pass

    def _prepare_level_page(self) -> None:
        # Some levels show custom alert overlays (e.g., "Next level" buttons) that block the DOM
        # after cookies resume a session. Click through them automatically so waits do not hang.
        keywords = ("next level", "continue", "start", "resume", "play")
        for _ in range(3):
            if not self._click_custom_alert_button(keywords):
                break

    def _click_custom_alert_button(self, keywords: Iterable[str]) -> bool:
        try:
            alert = self._driver.find_element(By.CSS_SELECTOR, "div.customAlert")
        except NoSuchElementException:
            return False

        buttons = alert.find_elements(By.TAG_NAME, "button")
        for button in buttons:
            label = button.text.strip().lower()
            if any(keyword in label for keyword in keywords):
                try:
                    button.click()
                    self._wait.until(EC.staleness_of(alert))
                except (WebDriverException, TimeoutException):
                    pass
                return True
        return False

    def submit_prompt(self, prompt: str, purpose: Optional[str] = None) -> str:
        if not prompt.strip():
            raise LakeraAgentError("prompt text cannot be empty")
        sanitized_prompt, changed = self._sanitize_sendable_text(prompt)
        if not sanitized_prompt.strip():
            raise LakeraAgentError("prompt became empty after removing unsupported characters")
        payload = {"purpose": purpose or "prompt", "prompt": sanitized_prompt}
        if changed:
            payload["original_prompt"] = prompt
            payload["sanitized"] = True
        self._last_prompt_error = None
        try:
            textarea = self._wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "textarea#comment"))
            )
            textarea.clear()
            textarea.send_keys(sanitized_prompt)
            previous_answer = self._find_answer_text()
            self._submit_form(textarea)
            result_type, answer = self._wait_for_prompt_result(previous=previous_answer)
            if result_type == "answer" and not answer.strip():
                deadline = time.time() + EMPTY_ANSWER_GRACE_SECONDS
                while time.time() < deadline:
                    time.sleep(EMPTY_ANSWER_POLL_SECONDS)
                    fresh = self._find_answer_text()
                    if fresh and (not previous_answer or fresh != previous_answer):
                        answer = fresh
                        break
        except TimeoutException as exc:
            error_message = "timed out waiting for prompt form"
            self._log_event("submit_prompt", payload, error=error_message)
            raise LakeraAgentError(error_message) from exc
        except LakeraAgentError as exc:
            self._log_event("submit_prompt", payload, error=str(exc))
            raise
        self.save_cookies()
        if result_type == "error":
            self._last_prompt_error = answer
            self._log_event("submit_prompt", payload, response=answer, extra={"result_type": "validation_error"})
        else:
            self._log_event("submit_prompt", payload, response=answer)
        return answer

    def submit_password(self, password: str, purpose: Optional[str] = None) -> str:
        if not password.strip():
            raise LakeraAgentError("password cannot be empty")
        sanitized_password, changed = self._sanitize_sendable_text(password)
        if not sanitized_password.strip():
            raise LakeraAgentError("password became empty after removing unsupported characters")
        payload = {"purpose": purpose or "password", "password": sanitized_password}
        if changed:
            payload["original_password"] = password
            payload["sanitized"] = True
        try:
            guess_input = self._wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "input#guess"))
            )
            guess_input.clear()
            guess_input.send_keys(sanitized_password)
            self._submit_form(guess_input)
            answer = self._wait_for_password_alert()
            next_level_url = self._capture_next_level_url()
            self._last_next_level_url = next_level_url
            self._dismiss_password_alerts()
        except TimeoutException as exc:
            error_message = "timed out waiting for password input"
            self._log_event("submit_password", payload, error=error_message)
            raise LakeraAgentError(error_message) from exc
        except LakeraAgentError as exc:
            self._log_event("submit_password", payload, error=str(exc))
            raise
        self.save_cookies()
        extra = {"next_level_url": self._last_next_level_url} if self._last_next_level_url else None
        self._log_event("submit_password", payload, response=answer, extra=extra)
        return answer

    @property
    def last_next_level_url(self) -> Optional[str]:
        return self._last_next_level_url
    
    @property
    def current_url(self) -> Optional[str]:
        if not self._driver:
            return None
        try:
            return self._driver.current_url
        except WebDriverException:
            return None

    @property
    def last_prompt_error(self) -> Optional[str]:
        return self._last_prompt_error
