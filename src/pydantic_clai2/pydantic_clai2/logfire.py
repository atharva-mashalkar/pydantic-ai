"""Default-enabled Logfire instrumentation, owned by the plugin rather than the process.

The settings menu is registered with `@host.configure`, so turning the plugin on opens it, as do `C` in
`/plugins` and `/plugins configure logfire`. Each edit is saved at once, and the loader loads the plugin again
when the menu closes, so the next run is traced with the new settings. Credentials never live in plugin
settings: the SDK reads `LOGFIRE_TOKEN` or its credential file, and the menu only says which one it found.
"""

import os
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal

import logfire
from anyio import CancelScope, to_thread
from opentelemetry.propagate import get_global_textmap, set_global_textmap
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from pydantic_ai.capabilities import Instrumentation
from pydantic_ai.models.instrumented import InstrumentationSettings

from . import theme
from .field_menu import TERMINAL, FieldMenu, FieldRow, Runners, first_error, run_flow
from .menu_worker import run_worker
from .plugins import PluginHost, SessionEnd

RUNNERS: Runners = TERMINAL
"""How the settings menu's widgets are shown; tests swap in scripted ones."""
CREDENTIALS_FILE = 'logfire_credentials.json'
"""The file the SDK writes on `logfire auth`/`projects use` and reads from `data_dir`."""


class LogfireSettings(BaseModel):
    """Non-secret telemetry options; credentials stay in Logfire's environment or credential file."""

    model_config = ConfigDict(extra='forbid', frozen=True, strict=True, hide_input_in_errors=True)
    service_name: str = Field(default='pydantic-clai2', min_length=1)
    send_to_logfire: Literal[False, 'if-token-present'] = 'if-token-present'
    include_content: bool = True
    include_binary_content: bool = True


def activate(host: PluginHost[None]) -> None:
    """Add core instrumentation without changing the supplied agent or global OTel providers."""
    config = host.settings(LogfireSettings)
    private_dir = logfire_dir()
    propagator = get_global_textmap()
    try:
        instance = logfire.configure(
            local=True,
            send_to_logfire=config.send_to_logfire,
            service_name=config.service_name,
            console=False,
            config_dir=private_dir,
            data_dir=private_dir,
        )
    finally:
        # Even local SDK configuration replaces the process-wide propagator.
        set_global_textmap(propagator)
    try:
        host.add(
            Instrumentation(
                settings=InstrumentationSettings(
                    tracer_provider=instance.config.get_tracer_provider(),
                    meter_provider=instance.config.get_meter_provider(),
                    include_content=config.include_content,
                    include_binary_content=config.include_binary_content,
                )
            )
        )
    except BaseException:
        _shutdown(instance)
        raise

    @host.on('session_end')
    async def shutdown(event: SessionEnd) -> None:
        with CancelScope(shield=True):
            finished = await to_thread.run_sync(_shutdown, instance)
            if not finished:
                host.console.print(
                    'Logfire shutdown timed out; some telemetry may not have been sent.',
                    style=theme.color(theme.WARNING),
                )

    @host.configure
    async def configure() -> str:
        messages = await run_worker(lambda: run_flow(FieldMenu(LogfireSource(host)), RUNNERS))
        return '\n'.join(messages) or 'Logfire settings unchanged.'


def logfire_dir() -> Path:
    """CLAI's private Logfire SDK directory: configuration and credentials are read only from here."""
    config_home = Path(os.getenv('XDG_CONFIG_HOME', '')).expanduser()
    if not config_home.is_absolute():
        config_home = Path.home() / '.config'
    return config_home / 'pydantic-clai2' / 'logfire'


def credentials() -> str:
    """Where the SDK will find a write token, as a short note for the menu."""
    if os.getenv('LOGFIRE_TOKEN'):
        return 'LOGFIRE_TOKEN is set'
    if (logfire_dir() / CREDENTIALS_FILE).is_file():
        return 'credentials file found'
    return 'no LOGFIRE_TOKEN or credentials file'


_BOOLEAN = ('true', 'false')
_SEND = FieldRow(
    key='send_to_logfire',
    label='Send to Logfire',
    description=(
        'Export traces when LOGFIRE_TOKEN is set or a logfire_credentials.json is in '
        '~/.config/pydantic-clai2/logfire/ (or under $XDG_CONFIG_HOME). Without credentials nothing is sent. '
        'Tokens are never stored in plugin settings.'
    ),
    default='if-token-present',
    choices=('if-token-present', 'false'),
    choice_labels={'if-token-present': 'when credentials are found', 'false': 'never'},
    allow_custom=False,
)
_ROWS = (
    _SEND,
    FieldRow(
        key='service_name',
        label='Service name',
        description='The OpenTelemetry service.name that CLAI traces are filed under in Logfire.',
        default='pydantic-clai2',
    ),
    FieldRow(
        key='include_content',
        label='Message content',
        description='Record prompts, responses, and tool arguments and results in spans.',
        default='true',
        choices=_BOOLEAN,
        choice_labels={'true': 'included', 'false': 'left out'},
        allow_custom=False,
    ),
    FieldRow(
        key='include_binary_content',
        label='Binary content',
        description='Record images, audio, and other file data in spans. Needs message content included.',
        default='true',
        choices=_BOOLEAN,
        choice_labels={'true': 'included', 'false': 'left out'},
        allow_custom=False,
    ),
)


class LogfireSource:
    """The settings menu's rows, read from and saved straight to the plugin's settings."""

    title = 'Logfire traces'

    def __init__(self, host: PluginHost[None]) -> None:
        """Every edit goes through `host.save_settings`."""
        self._host = host

    @property
    def settings(self) -> LogfireSettings:
        """The saved settings, including edits made earlier in this menu."""
        return self._host.settings(LogfireSettings)

    def rows(self) -> Sequence[FieldRow]:
        """Every option; sending notes where credentials would come from."""
        return [replace(_SEND, note=credentials()), *_ROWS[1:]]

    def current(self, row: FieldRow) -> str:
        """The value as the user would type it."""
        value: object = getattr(self.settings, row.key)
        return str(value).lower() if isinstance(value, bool) else str(value)

    def problem(self, row: FieldRow, text: str) -> str | None:
        """Validate against the whole settings model, as saving would."""
        try:
            self._updated(row, text)
        except ValidationError as exc:
            return first_error(exc)
        return None

    def apply(self, row: FieldRow, raw: str) -> str:
        """Save immediately; the loader loads the plugin again when the menu closes."""
        self._host.save_settings(self._updated(row, raw))
        return f'Saved {row.label}.'

    def reset(self, row: FieldRow) -> str:
        """Restore one option's default."""
        data = self.settings.model_dump(mode='json')
        del data[row.key]
        self._host.save_settings(LogfireSettings.model_validate(data))
        return f'Reset {row.label}.'

    def _updated(self, row: FieldRow, raw: str) -> LogfireSettings:
        value: JsonValue = raw == 'true' if row.choices and raw in _BOOLEAN else raw
        return LogfireSettings.model_validate({**self.settings.model_dump(mode='json'), row.key: value})


def _shutdown(instance: logfire.Logfire) -> bool:
    # SDK shutdown with flush=True can return on a flush timeout before stopping providers.
    try:
        flushed = instance.force_flush(timeout_millis=3000)
    finally:
        stopped = instance.shutdown(timeout_millis=3000, flush=False)
    return flushed and stopped
