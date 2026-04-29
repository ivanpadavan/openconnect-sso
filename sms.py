import json
import re
import subprocess
import threading

import structlog

logger = structlog.get_logger("sms")


class SmsCodeWatcher:
    def __init__(self, sms_config):
        sms_config.validate()
        self.config = sms_config
        self._process = None
        self._stopped = threading.Event()
        self._threads = []
        self._timer = None
        self._last_output = None

    def start(self, on_code, on_error=None):
        command = [
            self.config.imsg_path,
            "watch",
            "--chat-id",
            str(self.config.chat_id),
            "--json",
        ]
        logger.info("Starting SMS watcher", command=command)
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            message = f"Cannot start imsg: {exc}"
            logger.error(message)
            if on_error:
                on_error(message)
            return

        self._timer = threading.Timer(
            self.config.timeout_seconds,
            lambda: self._timeout(on_error),
        )
        self._timer.daemon = True
        self._timer.start()

        self._threads = [
            threading.Thread(
                target=self._read_stdout,
                args=(on_code, on_error),
                daemon=True,
            ),
            threading.Thread(
                target=self._read_stderr,
                args=(on_error,),
                daemon=True,
            ),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self):
        self._stopped.set()
        if self._timer:
            self._timer.cancel()
        if self._process and self._process.poll() is None:
            self._process.terminate()

    def _timeout(self, on_error):
        if self._stopped.is_set():
            return
        message = f"Timed out waiting for SMS code after {self.config.timeout_seconds}s"
        logger.warning(message)
        if on_error:
            on_error(message)
        self.stop()

    def _read_stdout(self, on_code, on_error):
        assert self._process.stdout is not None
        for line in self._process.stdout:
            if self._stopped.is_set():
                return
            try:
                code = extract_code(line, self.config)
            except ValueError as exc:
                message = line.strip()
                self._record_output(message)
                logger.warning(
                    "Ignoring non-JSON imsg stdout",
                    message=message,
                    error=str(exc),
                )
                continue
            if code:
                logger.info("SMS code received")
                on_code(code)
                self.stop()
                return
            logger.debug("Ignoring imsg JSON message without matching SMS code")

        if not self._stopped.is_set():
            rc = self._process.poll()
            message = f"imsg watch exited before receiving an SMS code (rc={rc})"
            if self._last_output:
                message += f"; last output: {self._last_output}"
            logger.error(message)
            if on_error:
                on_error(message)

    def _read_stderr(self, on_error):
        assert self._process.stderr is not None
        for line in self._process.stderr:
            message = line.strip()
            if not message:
                continue
            self._record_output(message)
            logger.warning("imsg stderr", message=message)

    def _record_output(self, message):
        self._last_output = message[:500]


def extract_code(line, sms_config):
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(exc) from exc

    if message.get("is_from_me"):
        logger.debug("Ignoring outgoing imsg message")
        return None

    text = message.get("text") or ""
    match = re.search(sms_config.regex, text)
    if not match:
        logger.debug("Ignoring imsg message that does not match regex", text=text)
        return None
    return match.group(1) if match.groups() else match.group(0)
