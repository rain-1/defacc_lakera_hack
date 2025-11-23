import anthropic
import os
import re

class ClaudeAgent(object):

    def __init__(self, model):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        self.client = anthropic.Anthropic(
            api_key = api_key
        )
        self.messages = []
        self.model = model
        #self.first = True
        # state vars filled by parsing Lakera responses
        self.prompt = None
        self.password = None
        self.success = False


    def load_task_description(self, task_description, filename="prompts/prompt_hand.txt"):
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

    def load_task_description_wipe(self, task_description, filename="prompts/prompt_hand.txt"):
        """
        Wipes previous conversation and adds task description from Lakera in conversation.
        Reads initial prompt.
        """
        self.success = False
        self.messages = []
        try:
            with open(filename, "r", encoding="utf-8") as f:
                prompt_text = f.read().strip()
        except FileNotFoundError:
            raise FileNotFoundError(f"Initial prompt file not found: {filename}")
        prompt_text += '\n' + task_description
        self.messages.insert(0, {"role": "user", "content": prompt_text})


    def model_turn(self):
        message = self.client.messages.create(
            model=self.model,
            max_tokens=20000,
            temperature=1,
            messages=self.messages
        ).content[0].text
        print("[claude] Model output:" + message)
        self.messages.append({"role": "assistant", "content": message})

        m_prompt = re.search(r"<prompt>(.*?)</prompt>", message, flags=re.IGNORECASE | re.DOTALL)
        m_password = re.search(r"<password>(.*?)</password>", message, flags=re.IGNORECASE | re.DOTALL)
        if m_prompt:
            prompt_val = m_prompt.group(1).strip()
            self.prompt = prompt_val 
            return "prompt"       
        elif m_password:
            password_val = m_password.group(1).strip()
            self.password = password_val
            return "password"
        else:
            raise RuntimeError(f"Model did not produce <prompt> or <password> tag.")

    def process_lakera_output(self, answer=None, check=None):
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
                message = "You guessed the password!"
                self.success = True
            else:
                message = "Wrong password! Change your prompt."
            tagged = f"<check>{message}</check>"
            self.messages.append({"role": "assistant", "content": tagged})
            extracted.append(("check", inner))

