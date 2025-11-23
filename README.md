# defacc_lakera_hack

## Usage
- Run `password_probe.py --password <guess>` to submit a password via Selenium.
- Run `level2_probe.py` to walk through the warm-up prompt, submit the level 1 password, and stay in the same browser session while jumping to level 2 (cookies/localStorage persist under `userdata/`).
- Run `agent.py --template prompts/main.txt --openrouter-key <key>` for the autonomous loop; it keeps a single browser alive, advances through subsequent levels automatically, and logs transcripts plus state under `userdata/`.
- On a correct guess, `LakeraAgent` now clicks the in-page "Next Level" button, records the resulting URL, and prints it so you can jump directly to the next challenge.

## Setup
1. Ensure Python 3.11+ is installed.
2. Create a virtual environment (once per clone):
	```bash
	python3 -m venv .venv
	```
3. Activate it:
	```bash
	source .venv/bin/activate
	```
4. Install dependencies:
	```bash
	pip install -r requirements.txt
	```
5. Run the probes (e.g. `python password_probe.py --password COCOLOCO`).

## Data storage
- Runtime artifacts such as `cookies.json`, `interactions.jsonl`, `latest-level.url`, `lakera-storage.json`, and rolling transcripts now live under `userdata/`.
- Remove the `userdata/` directory if you want to reset local state; it will be recreated automatically on the next run.


## Ubuntu on WSL

```
sudo apt remove chromium-browser
sudo apt install chromium-chromedriver fonts-liberation
```
