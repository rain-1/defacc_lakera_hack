import anthropic
import os
import re

class ClaudeAgent(object):

    def __init__(self):
        self.client = anthropic.Anthropic(
            api_key = os.environ.get("ANTHROPIC_API_KEY")
        )
        self.messages = []
        self.first = True
        # state vars filled by parsing Lakera responses
        self.prompt = None
        self.password = None
        self.success = False


    def load_task_description(self, task_description, filename="prompt_hand.txt"):
        """
        Adds task description from Lakera in conversation.
        Reads initial prompt at the start.
        """
        self.success = False
        if self.first:
            try:
                with open(filename, "r", encoding="utf-8") as f:
                    prompt_text = f.read().strip()

            except FileNotFoundError:
                raise FileNotFoundError(f"Initial prompt file not found: {filename}")
        self.first = False
        prompt_text += '\n' + task_description
        self.messages.insert(0, {"role": "user", "content": prompt_text})


    def model_turn(self):
        message = self.client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=20000,
            temperature=1,
            messages=self.messages
        )
        self.messages.append(message)

        m_prompt = re.search(r"<prompt>(.*?)</prompt>", message.content, flags=re.IGNORECASE | re.DOTALL)
        m_password = re.search(r"<password>(.*?)</password>", message.content, flags=re.IGNORECASE | re.DOTALL)
        if m_prompt:
            prompt_val = m_prompt.group(1).strip()
            self.prompt = prompt_val 
            return "prompt"       
        elif m_password:
            password_val = m_password.group(1).strip()
            self.password = password_val
            return "passowrd"
        else:
            raise RuntimeError(f"Model did not produce <prompt> or <password> tag.")

    def extract_and_add_tags(self, answer=None, check=None):
        """
        If answer or check (strings) are provided (not None), add them to self.messages
        as assistant messages wrapped in their respective tags.
        Returns list of tuples (tag, inner_content).
        """
        extracted = []
        if answer is not None:
            inner = answer.strip()
            tagged = f"<answer>{inner}</answer>"
            self.messages.append({"role": "assistant", "content": tagged})
            extracted.append(("answer", inner))
        elif check is not None:
            inner = check.strip()
            if "You guessed the password!" in inner:
                self.success = True
            else:
                inner.replace("to bypass the defenses", "")
            tagged = f"<check>{inner}</check>"
            self.messages.append({"role": "assistant", "content": tagged})
            extracted.append(("check", inner))

    def parse_model_output_vars(self, model_output):
        """
        If model_output contains <prompt>...</prompt> save to self.prompt.
        If it contains <password>...</password> save to self.password.
        Returns (prompt, password) where missing values are None.
        """
        prompt_val = None
        password_val = None

        m_prompt = re.search(r"<prompt>(.*?)</prompt>", model_output, flags=re.IGNORECASE | re.DOTALL)
        if m_prompt:
            prompt_val = m_prompt.group(1).strip()
            self.prompt = prompt_val
        else:
            self.prompt = None

        m_password = re.search(r"<password>(.*?)</password>", model_output, flags=re.IGNORECASE | re.DOTALL)
        if m_password:
            password_val = m_password.group(1).strip()
            self.password = password_val
        else:
            self.password = None

        return prompt_val, password_val

