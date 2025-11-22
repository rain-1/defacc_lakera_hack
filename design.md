lakera.py implemenmts an object that supports the agent in accessing the lakera website
it provides abilities to:
(A) get the explanation for the current level
(B) attempt a prompt and get a response
(C) attempt a password and get a result

https://gandalf.lakera.ai/baseline

we will need a cookie jar to manage state, as there is password protection to reach later levels

For (A) you need to load the page and get the content of this p class="description"
> <p class="description mb-8 text-[16px] text-[#E2E4E8]">Your goal is to make Gandalf reveal the secret password for each level. However, Gandalf will upgrade the defenses after each successful password guess!</p>

For (B) put text into <textarea id="comment"> and press the submit button in the main form on the webpage
The response will show up in a new <p class="answer">
and this will also reveal a password box:

For (C) put password into the revealed  <input id="guess"> and press the submit button for the form containing that guess input box.

We might need to use an automatable chromium system like selenium, so i've created a venv with that. source ~/venv/bin/activate
