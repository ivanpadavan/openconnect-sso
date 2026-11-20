# OpenConnect SSO

This fork of [OpenConnect SSO](https://github.com/vlaci/openconnect-sso) adds autofill for codes received through macOS Messages.

## Install from GitHub

Run this in a Python virtual environment with Git and OpenConnect installed on your system.

```sh
python -m pip install "openconnect-sso @ git+https://github.com/ivanpadavan/openconnect-sso.git@main"
openconnect-sso --help
```

Replace `main` with a branch, tag, or commit hash to select a version. From a local checkout, use `python -m pip install .`.

## SSO browser

Set the action limit at the top level of `~/.config/openconnect-sso/config.toml`, before any named sections.

```toml
max_auto_actions = 7
```

The default is seven completed autofill changes or clicks per login attempt. Page loads and opening popups use none of that budget. If another automatic action would exceed the limit, the program reports an error and exits with status `22`. Set the limit to `0` to disable automatic actions.

Pass `--debug-html` to write the full HTML of each successfully loaded SSO page to the application log. The output can contain sensitive data.
