"""Selenium-backed helper for interacting with https://gandalf.lakera.ai."""

from __future__ import annotations

import json
from pathlib import Path
from shutil import which
from typing import Optional

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
    ) -> None:
        self._base_url = base_url
        self._cookie_jar = cookie_jar
        self._timeout = timeout
        self._headless = headless
        self._chrome_binary = chrome_binary
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

        driver.get(self._base_url)
        if self._cookie_jar and self._cookie_jar.exists():
            self._load_cookies(driver)
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

    def save_cookies(self) -> None:
        if not self._cookie_jar:
            return
        cookies = self._driver.get_cookies()
        self._cookie_jar.parent.mkdir(parents=True, exist_ok=True)
        self._cookie_jar.write_text(json.dumps(cookies))

    def close(self, *, save_state: bool = True) -> None:
        if save_state:
            self.save_cookies()
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

    def describe_level(self) -> str:
        try:
            self._driver.get(self._base_url)
            description = self._wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "p.description"))
            )
        except TimeoutException as exc:
            raise LakeraAgentError("could not load level description") from exc
        return description.text.strip()

    def submit_prompt(self, prompt: str) -> str:
        if not prompt.strip():
            raise LakeraAgentError("prompt text cannot be empty")
        textarea = self._wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "textarea#comment"))
        )
        textarea.clear()
        textarea.send_keys(prompt)
        self._submit_form(textarea)
        answer = self._wait_for_answer()
        self.save_cookies()
        return answer

    def submit_password(self, password: str) -> str:
        if not password.strip():
            raise LakeraAgentError("password cannot be empty")
        guess_input = self._wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "input#guess"))
        )
        guess_input.clear()
        guess_input.send_keys(password)
        self._submit_form(guess_input)
        answer = self._wait_for_password_alert()
        self._dismiss_password_alerts()
        self.save_cookies()
        return answer
