# Run the gym booking bot through Throne

This guide records the proxy setup that worked on your Linux laptop. Keep Throne connected while the bot runs.

## Start the bot

Open a terminal in your project folder and run these commands one at a time.

```bash
cd ~/Desktop/Projects/gymbookingbot
export HTTP_PROXY=http://127.0.0.1:2080
export HTTPS_PROXY=http://127.0.0.1:2080
.venv/bin/python bot.py
```

Open your bot in Telegram and send `/start` or `/admin` to verify it responds. Press Ctrl+C in the terminal to stop the bot.

The export commands apply to the current terminal and programs started from it. Repeat them when you open a new terminal. They stay set when you stop and restart the bot in that same terminal.

## What the commands mean

| Part | Meaning |
| --- | --- |
| `export` | Makes an environment variable available to programs launched from this terminal. |
| `HTTP_PROXY` | Sets the proxy used for HTTP requests by programs that support this variable. |
| `HTTPS_PROXY` | Sets the proxy used for HTTPS requests by programs that support this variable. |
| `127.0.0.1` | Your own computer, where Throne runs. |
| `2080` | Throne's local proxy port in this setup. |
| `.venv/bin/python bot.py` | Runs the bot using the project's Python environment. |

The bot's HTTPX networking library reads these proxy variables. Requests travel from your bot through Throne to Telegram.

## Check the connection

With your Throne profile connected, run this command. It does not need your bot token.

```bash
curl -I --proxy http://127.0.0.1:2080 --connect-timeout 10 --max-time 20 https://api.telegram.org
```

`HTTP/1.1 200 Connection established` means the proxy accepted the tunnel. Wait for the final response too. An HTTP response from Telegram confirms the connection reached the service. A later timeout still indicates a connection problem.

| Result | What to check |
| --- | --- |
| Connection refused | Throne must be running a connected profile and listening on the port you specified. |
| Connection timed out | Check the active Throne profile, its server, and the network connection. |
| Bot times out after curl succeeds | Run both export commands in the same terminal you use to launch the bot. |

In Throne, check Settings → Basic Settings → Common → Inbound Settings → Listen Port. If it differs from 2080, replace 2080 in all commands above with your actual port.

## Set up a new computer

In the project folder, create the environment and install the saved dependencies.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

If Ubuntu reports that ensurepip is unavailable, install the venv package matching your Python version, then retry environment creation. For Python 3.12, the package is `python3.12-venv`.

Create a local `.env` beside `bot.py` containing your own token.

```dotenv
BOT_TOKEN=YOUR_FULL_BOTFATHER_TOKEN
```

Keep `.env` excluded through `.gitignore`. Then connect Throne and use the startup commands at the top of this guide.

## Save this guide in your repository

Download this file into your local project's `docs` folder. Once it is saved there, run the following from the project folder.

```bash
git add docs/telegram-proxy-setup.md
git commit -m "Document local Telegram bot proxy setup"
git push
```

The proxy environment settings themselves require no Git commit.

## References

- [Throne proxy modes and local port settings](https://throneproj.github.io/guides/proxy_modes/)
- [HTTPX environment variables](https://www.python-httpx.org/environment_variables/)
