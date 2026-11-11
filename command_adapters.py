"""Delivery adapters. A queue receipt is not evidence of session execution."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


class DeliveryError(Exception):
    def __init__(self, code, uncertain=False):
        self.code, self.uncertain = code, uncertain
        super().__init__(code)


class CodexAppAdapter:
    """Queue on the original session without opening or focusing any App window.

    No separate app-server, new session, model override, or App database write.
    This first adapter supports macOS; other runtimes implement the same submit
    contract and must supply evidence from their bound original session.
    """
    mode = 'background_queue'
    def __init__(self, settings=None):
        self.settings = settings or {}
        self.checked_at, self.binary, self.reason = None, None, 'adapter_unavailable'

    def available(self):
        if self.checked_at is not None and time.monotonic() - self.checked_at < 30:
            return self.binary is not None
        self.checked_at = time.monotonic()
        self.binary = None
        if sys.platform != 'darwin':
            self.reason = 'platform_not_supported'
            return False
        selected = self.settings.get('binary')
        candidates = [selected] if selected else [
            '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex',
            '/Applications/Codex.app/Contents/Resources/codex', shutil.which('codex')]
        for candidate in candidates:
            if not candidate or not Path(candidate).is_absolute() or not os.access(candidate, os.X_OK):
                continue
            try:
                result = subprocess.run([candidate, 'queue', '--help'], stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and 'Queue a message for an existing session' in result.stdout and '--thread' in result.stdout:
                    self.binary, self.reason = candidate, None
                    return True
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.reason = 'codex_queue_unavailable'
        return False

    def submit(self, session_id, text):
        if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', session_id):
            raise DeliveryError('invalid_session')
        if not self.available():
            raise DeliveryError(self.reason)
        try:
            result = subprocess.run([self.binary, 'queue', '--thread', session_id, '--message', text],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            raise DeliveryError('delivery_unknown', uncertain=True)
        except OSError:
            raise DeliveryError('adapter_unavailable')
        match = re.fullmatch(r'Queued message ([0-9a-f-]{36}) for thread ' + re.escape(session_id) + r'\.\s*', result.stdout)
        if result.returncode != 0 or not match:
            # A process failure can happen after the native queue committed.
            raise DeliveryError('delivery_unknown', uncertain=True)
        # A loaded session can consume this queue in the background. An unloaded
        # session remains queued until the user loads it. Never use a deep link
        # or UI automation here: even `open -g` can foreground the App's window.
        return {'native_message_id': match.group(1), 'delivery_mode': self.mode}


def make_adapters(config, on_event=None):
    from hermes_acp import HermesAcpAdapter
    from platform_adapters import CursorCloudAdapter, GrokWebhookAdapter
    settings = config.get('command_adapters', {})
    return {'codex_app': CodexAppAdapter(settings.get('codex_app')),
            'hermes_acp': HermesAcpAdapter(config, on_event),
            'cursor_cloud': CursorCloudAdapter(config, on_event),
            'grok_webhook': GrokWebhookAdapter(config, on_event)}
