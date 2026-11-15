"""Settings and hardware profiles.

Everything the pipeline is allowed to vary by machine comes from the active
profile. Code never branches on "is this the 3090": it reads a number.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

DEFAULT_PROFILES = Path(__file__).with_name("default_profiles.yaml")
LOCAL_BINDS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class Profile:
    name: str
    stt_model: str
    stt_device: str
    stt_compute_type: str
    llm_model: str
    llm_context: int
    chunk_minutes: float
    chunk_tokens: int
    reduce_input_tokens: int
    max_output_tokens: int
    stt_fallback_model: str | None = None
    llm_num_gpu: int | None = None
    description: str = ""

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> "Profile":
        known = {f for f in cls.__dataclass_fields__ if f != "name"}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"profile {name!r}: unknown keys {sorted(unknown)}")
        p = cls(name=name, **d)
        # The map step must fit its prompt plus its answer in the window;
        # failing at load time beats a context overflow at hour three.
        if p.chunk_tokens + p.max_output_tokens + 1500 > p.llm_context:
            raise ValueError(f"profile {name!r}: chunk_tokens + max_output_tokens exceeds llm_context")
        if p.reduce_input_tokens + p.max_output_tokens + 1500 > p.llm_context:
            raise ValueError(f"profile {name!r}: reduce_input_tokens + max_output_tokens exceeds llm_context")
        return p


@dataclass
class Settings:
    vault: Path
    profile: Profile
    ollama_host: str
    bind: str = "127.0.0.1"
    port: int = 8765
    password: str | None = None
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    profiles: dict[str, Profile] = field(default_factory=dict)

    @property
    def requires_login(self) -> bool:
        return self.bind not in LOCAL_BINDS

    @property
    def campaign_dir(self) -> Path:
        return self.vault / "campaign"

    @property
    def sessions_dir(self) -> Path:
        return self.vault / "sessions"

    @property
    def db_path(self) -> Path:
        return self.vault / "app.db"

    def ensure_dirs(self) -> None:
        for d in (self.vault, self.campaign_dir, self.sessions_dir, self.vault / "config", self.vault / "exports"):
            d.mkdir(parents=True, exist_ok=True)


def is_private_host(url: str) -> bool:
    """True when the Ollama host is this machine or a private-network address.

    R1/R2: sending a transcript to a public address would be an outbound
    request, which is exactly what offline mode forbids.
    """
    host = urlparse(url).hostname or ""
    if host in LOCAL_BINDS or host.endswith(".local") or host.endswith(".lan") or "." not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def load_settings(vault: str | Path | None = None, **overrides: Any) -> Settings:
    vault_path = Path(vault or os.environ.get("SCRIBE_VAULT", "vault")).resolve()
    profiles_file = vault_path / "config" / "profiles.yaml"
    if not profiles_file.exists():
        profiles_file.parent.mkdir(parents=True, exist_ok=True)
        profiles_file.write_text(DEFAULT_PROFILES.read_text(encoding="utf-8"), encoding="utf-8")
    raw = yaml.safe_load(profiles_file.read_text(encoding="utf-8")) or {}
    profiles = {n: Profile.from_dict(n, d) for n, d in (raw.get("profiles") or {}).items()}
    active = overrides.pop("profile", None) or os.environ.get("SCRIBE_PROFILE") or raw.get("active")
    if active not in profiles:
        raise ValueError(f"active profile {active!r} not in {sorted(profiles)}")
    ollama_host = overrides.pop("ollama_host", None) or os.environ.get("SCRIBE_OLLAMA_HOST") or raw.get("ollama_host", "http://127.0.0.1:11434")
    s = Settings(
        vault=vault_path,
        profile=profiles[active],
        ollama_host=ollama_host.rstrip("/"),
        bind=os.environ.get("SCRIBE_BIND", "127.0.0.1"),
        port=int(os.environ.get("SCRIBE_PORT", "8765")),
        password=os.environ.get("SCRIBE_PASSWORD") or None,
        ssl_certfile=os.environ.get("SCRIBE_SSL_CERT") or None,
        ssl_keyfile=os.environ.get("SCRIBE_SSL_KEY") or None,
        profiles=profiles,
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    s.ensure_dirs()
    return s


def set_active_profile(settings: Settings, name: str) -> None:
    if name not in settings.profiles:
        raise ValueError(f"unknown profile {name!r}")
    f = settings.vault / "config" / "profiles.yaml"
    raw = yaml.safe_load(f.read_text(encoding="utf-8"))
    raw["active"] = name
    f.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    settings.profile = settings.profiles[name]
