# GeekPulse

English · [简体中文](README.zh-CN.md)

GeekPulse monitors models on your [Sub2API](https://github.com/Wei-Shaw/sub2api) gateway with two scheduled tests:

- **Reasoning:** Repeat a question to see whether a model still gets it right. Use the built-in candy puzzle or give it your own question with a known answer.
- **Drawing:** Compare what the model draws from the same prompt over time. The built-in prompt asks for an SVG of a pelican riding a bicycle.

[Install on your gateway](#install-on-your-gateway) · [Run the tests locally](#run-the-tests-locally)

The dashboard and setup guides are in Simplified Chinese. GeekPulse targets [Sub2API v0.2.23](docs/upstream.md).

## Install on your gateway

Run these commands on the Linux host that serves your Sub2API site. The installer expects Nginx to serve HTTPS on that host and Sub2API to listen on a local HTTP port. You’ll need systemd, sudo, Bash, Git, curl, and `/usr/bin/python3` (3.11 or later, with venv).

```sh
curl -fL --proto '=https' --proto-redir '=https' -o geekpulse-install.sh https://github.com/zhanglx-kiko/GeekPulse/releases/latest/download/geekpulse-install.sh &&
sudo bash geekpulse-install.sh
```

Enter your site’s domain and **Sub2API’s local HTTP port** (press Enter for `8080`). Type `y` to install GeekPulse and its updater. GeekPulse uses `127.0.0.1:8091` for its own service.

In your existing Nginx **HTTPS `server` block for Sub2API**, add `include /etc/geekpulse/nginx-location.conf;` and run `sudo nginx -t && sudo systemctl reload nginx`. Sign in to Sub2API and open `/model-lab/`. You can also add this page to the site menu. The installer doesn’t edit your virtual hosts or restart Sub2API.

See the [installation guide](docs/deployment.md) for configuration and health checks. Installing GeekPulse doesn’t make any model requests.

## Reading the dashboard

For reasoning checks, each group shows recent answers, token counts reported by the provider, and the average time to first token. Say a group ran only the candy puzzle in the past 24 hours: one right answer, one wrong answer, and one timeout.

| 24-hour metric | Result | What counts |
| --- | --- | --- |
| Request success | 2/3 | Two requests returned an answer, including the wrong one. |
| Answer pass rate (`不降智率`) | 1/2 | One of those two answers was correct. The timeout had no answer to grade. |

In the same group, **图片检测** (drawings) shows up to 60 recent drawings per probe, newest first. Open a thumbnail to see the full HTML or SVG drawing. GeekPulse doesn’t score drawings or count them toward the [answer metrics](docs/contract.md).

To try either test on your own, open **自助测试** (personal tests) and use your own API key with Responses or Chat Completions. Personal runs don’t change the group’s numbers.

## Monitor a model

Sign in with your Sub2API account to see the groups you have access to. Only site administrators can manage probes.

1. In `/model-lab/`, open **探测管理** (probe management) and create a probe. Pick a group, a model, and either a reasoning or a drawing check. A new probe uses the group’s name and starts paused by default.
2. Run it once by hand or turn on its schedule. Either can incur model charges. Once you publish, enable, or run the probe, you can’t change its group, model, test type, preset, prompt, or expected answer. You can still change how often it runs.
3. Create a new probe when you switch models. When you retire the old model, filter by **探测名称** (probe name) and check the model in that row before deleting it. Deleting a probe removes its answers and drawings from the dashboard and its answers from the totals. Other probes with the same name keep their history.

## Updates

When a new release is available, open **探测管理 → 版本更新** and click **升级**. GeekPulse checks GitHub every 15 minutes. Leave your probes on; their schedules stay as you set them. Only GeekPulse restarts. A test interrupted by the restart won’t be retried or counted as a wrong answer, though the provider may still charge for the request.

## Run the tests locally

You’ll need Git, Python 3.11, and Node.js. From the repository root on Linux or macOS:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -B -m unittest discover -s tests -q
node --test tests/test_frontend_session.cjs tests/test_frontend_interaction.cjs tests/test_frontend_theme.cjs
```

On Windows, create the environment with `py -3.11 -m venv .venv` and use `.\.venv\Scripts\python.exe` in place of `.venv/bin/python`. The Node command is the same.

Once you’ve installed the dependencies, the tests run offline with temporary SQLite databases and mock model responses. You don’t need a Sub2API account or a model key. Only the frontend tests use Node.js; there’s nothing to install with npm or build for the browser.

## License

[MIT](LICENSE) · © 2026 zhanglx · <changlinghsiao520@gmail.com>
