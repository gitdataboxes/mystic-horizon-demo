from __future__ import annotations

import unittest

from mystic.types import derive_source


class SourceTests(unittest.TestCase):
    def test_extraction_context_maps_to_post_call(self) -> None:
        self.assertEqual(derive_source("public", "write-fact", {"isExtraction": True}), "post-call")

    def test_scheduler_context_maps_to_agent(self) -> None:
        self.assertEqual(derive_source("public", "write-fact", {"isScheduler": True}), "agent")

    def test_cli_context_maps_to_cli(self) -> None:
        self.assertEqual(derive_source("public", "write-fact", {"isCli": True}), "cli")

    def test_public_action_write_maps_to_caller(self) -> None:
        self.assertEqual(derive_source("public", "write-action"), "caller")

    def test_owner_context_maps_to_owner(self) -> None:
        self.assertEqual(derive_source("owner", "read-facts"), "owner")

    def test_default_mid_call_source_is_used_otherwise(self) -> None:
        self.assertEqual(derive_source("public", "write-fact"), "mid-call")
