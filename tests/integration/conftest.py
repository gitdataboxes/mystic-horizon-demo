from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio

from mystic.actions import drain_scheduler, stop_scheduler
from mystic.calls import (
    drain_pending_extraction_tasks,
    reset_active_calls,
    set_call_ended_callback,
    set_extraction_pipeline,
)
from mystic.db import close_database, initialize_schema, open_database
from mystic.memory import drain_retry_loop
from mystic.server import clear_rate_limit_store
from mystic.skills import init_skills, reset_registry
from tests.integration.helpers import IntegrationEnv
from tests.python_helpers import TempAppHome, seed_core_files


@pytest_asyncio.fixture()
async def integration_env() -> AsyncIterator[IntegrationEnv]:
    with TempAppHome() as home:
        seed_core_files(home)
        db = open_database(":memory:")
        initialize_schema(db)
        reset_active_calls(db)
        reset_registry()
        init_skills()
        set_extraction_pipeline(None)
        set_call_ended_callback(None)
        clear_rate_limit_store()
        try:
            yield IntegrationEnv(home=home, db=db)
        finally:
            await drain_scheduler(1000)
            await drain_retry_loop(1000)
            await drain_pending_extraction_tasks(1000)
            stop_scheduler()
            clear_rate_limit_store()
            set_extraction_pipeline(None)
            set_call_ended_callback(None)
            reset_active_calls(db)
            reset_registry()
            close_database(db)
