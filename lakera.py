"""Selenium-backed helper for interacting with https://gandalf.lakera.ai."""

from __future__ import annotations

import json
from pathlib import Path
from shutil import which
from typing import Iterable, Optional
from datetime import datetime

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


class LakeraAgentError(RuntimeError):
    """General failure interacting with the Lakera page."""


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
        log_path: Optional[Path] = Path("interactions.jsonl"),
        storage_path: Optional[Path] = None,
        page_load_stop_after: float = 5.0,
    ) -> None:
        self._base_url = base_url
        self._cookie_jar = cookie_jar
        self._timeout = timeout
        self._headless = headless
        self._chrome_binary = chrome_binary
        self._log_path = log_path
        self._storage_path = storage_path
        self._page_load_stop_after = max(0.0, page_load_stop_after)
        self._last_next_level_url: Optional[str] = None
        self._driver = self._build_driver()
        self._wait = WebDriverWait(self._driver, self._timeout)

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
        submit.click()

    def _wait_for_answer(self, previous: Optional[str] = None) -> str:
        def _answer_updated(driver: webdriver.Chrome) -> Optional[str]:
            text = self._find_answer_text()
            if text is None:
                return None if previous else ""
            if not previous or text != previous:
                return text
            return False

        try:
            result = self._wait.until(_answer_updated)
        except TimeoutException as exc:
            raise LakeraAgentError("timed out waiting for answer") from exc
        return result or ""

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
        for attempt in (1, 2):
            try:
                self._prepare_level_page()
                description = self._wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "p.description"))
                )
                text = description.text.strip()
                break
            except TimeoutException:
                if attempt == 2:
                    error_message = "could not load level description"
                    self._log_event("describe_level", payload, error=error_message)
                    raise LakeraAgentError(error_message)
                self._driver.refresh()
        else:
            raise LakeraAgentError("unreachable describe_level state")
        self._log_event("describe_level", payload, response=text)
        return text

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
        payload = {"purpose": purpose or "prompt", "prompt": prompt}
        try:
            textarea = self._wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "textarea#comment"))
            )
            textarea.clear()
            textarea.send_keys(prompt)
            self._submit_form(textarea)
            answer = self._wait_for_answer()
        except TimeoutException as exc:
            error_message = "timed out waiting for prompt form"
            self._log_event("submit_prompt", payload, error=error_message)
            raise LakeraAgentError(error_message) from exc
        except LakeraAgentError as exc:
            self._log_event("submit_prompt", payload, error=str(exc))
            raise
        self.save_cookies()
        self._log_event("submit_prompt", payload, response=answer)
        return answer

    def submit_password(self, password: str, purpose: Optional[str] = None) -> str:
        if not password.strip():
            raise LakeraAgentError("password cannot be empty")
        payload = {"purpose": purpose or "password", "password": password}
        try:
            guess_input = self._wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "input#guess"))
            )
            guess_input.clear()
            guess_input.send_keys(password)
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
