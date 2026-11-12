"""Optional runners for models that are not called through LiteLLM.

The benchmark calls models through LiteLLM (`core/model_adapter.py`). A model entry whose
`provider` names an adapter is run by that adapter instead, which is how a setup that has
no API (a CLI, a desktop app, an agent framework) can be benchmarked without changing the
harness. An adapter is a module in this package with:

    PROVIDER = "my-runner"                      # the `provider` of its model entries

    async def preflight(conn, fault_active) -> str | None
        # None when it can run, otherwise what the user has to do (checked once at start)

    async def run_case(cfg, conn, case, offered, server_tools, *, tool_profile, run_index,
                       max_steps, system_prompt) -> CaseRun
        # run one case and return what happened, as core/agent_loop.py does

Adding a module here is enough: adapters are found by their PROVIDER. Model entries for an
adapter belong in config/models.local.yaml, not in the shared models.yaml.
"""

from __future__ import annotations

import importlib
import pkgutil
from functools import lru_cache
from types import ModuleType


@lru_cache(maxsize=1)
def _adapters() -> dict[str, ModuleType]:
    found = {}
    for info in pkgutil.iter_modules(__path__):
        module = importlib.import_module(f"{__name__}.{info.name}")
        provider = getattr(module, "PROVIDER", None)
        if provider:
            found[provider] = module
    return found


def for_provider(provider: str) -> ModuleType | None:
    """The adapter that runs models of this provider, or None for the LiteLLM loop."""
    return _adapters().get(provider)
