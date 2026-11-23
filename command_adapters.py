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
    macOS and Windows runtimes must expose the same native queue capability
    and supply evidence from their bound original session.
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
        if sys.platform not in {'darwin', 'win32'}:
            self.reason = 'platform_not_supported'
            return False
        selected = self.settings.get('binary')
        executable = 'codex.exe' if sys.platform == 'win32' else 'codex'
        candidates = [selected] if selected else [shutil.which(executable)]
        if not selected and sys.platform == 'darwin':
            candidates[:0] = [
                '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex',
                '/Applications/Codex.app/Contents/Resources/codex',
            ]
        for candidate in candidates:
            if not candidate or not Path(candidate).is_absolute() or not os.access(candidate, os.X_OK):
                continue
            # Windows can route .cmd/.bat through a command shell even when
            # shell=False. Queue text must reach a native executable literally.
            if sys.platform == 'win32' and Path(candidate).suffix.lower() != '.exe':
                continue
            try:
                result = subprocess.run([candidate, 'queue', '--help'], stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=5,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if sys.platform == 'win32' else 0)
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
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if sys.platform == 'win32' else 0)
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


def make_adapters(config, on_event=None, history=None):
    from hermes_acp import HermesAcpAdapter
    from platform_adapters import CursorCloudAdapter, GrokWebhookAdapter
    from cursor_local import CursorLocalAdapter
    from codex_background import CodexBackgroundAdapter
    from grok_sdk import GrokSdkAdapter
    from openrouter_free import OpenRouterAdapter
    settings = config.get('command_adapters', {})
    return {'codex_app': CodexAppAdapter(settings.get('codex_app')),
            'hermes_acp': HermesAcpAdapter(config, on_event),
            'cursor_cloud': CursorCloudAdapter(config, on_event),
            'cursor_local': CursorLocalAdapter(config, on_event),
            'codex_background': CodexBackgroundAdapter(config, on_event),
            'grok_webhook': GrokWebhookAdapter(config, on_event),
            'openrouter': OpenRouterAdapter(config, on_event, history),
            'grok_sdk': GrokSdkAdapter(config, on_event)}
