from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from mystic.config import SmtpConfig, get_agent_config, list_journal_entries, read_soul, write_soul
from mystic.phone import CapabilityReadiness, PhoneReadiness
from mystic.types import SearchResult, OperationalContext
from mystic.db import get_action_by_id, get_fact_by_id, insert_action, insert_call, update_call_end, update_call_summary, close_database, initialize_schema, open_database, get_active_facts_by_person, insert_fact, get_person_by_phone, upsert_person
from mystic.skills import init_skills, load_handler_module, reset_registry
from tests.python_helpers import TempAppHome, seed_core_files, make_embedding


def _smtp_config() -> SmtpConfig:
    return SmtpConfig(
        host="smtp.example.com",
        port=587,
        username="user",
        password="pass",
        from_address="agent@example.com",
    )


def _phone_ready(url: str = "https://agent.tail1234.ts.net") -> PhoneReadiness:
    return PhoneReadiness(
        status="ok",
        public_url=url,
        phone_number="+15550004444",
        phone_number_sid="PN123",
        tailscale=CapabilityReadiness("ok"),
        funnel=CapabilityReadiness("ok"),
        twilio=CapabilityReadiness("ok"),
    )


class SkillHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        self.db = open_database(":memory:")
        initialize_schema(self.db)
        reset_registry()
        self.registry = init_skills()

        self.person = upsert_person(self.db, "+15550001111", "Alice")
        self.call = insert_call(
            self.db,
            person_id=self.person.id,
            direction="inbound",
            audience="owner",
            call_id="call-123",
        )
        self.owner_ctx = OperationalContext(
            audience="owner",
            call_id=self.call.id,
            person_id=self.person.id,
            source="mid-call",
        )
        self.public_ctx = OperationalContext(
            audience="public",
            call_id=self.call.id,
            person_id=self.person.id,
            source="mid-call",
        )

    def tearDown(self) -> None:
        reset_registry()
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    def handler(self, skill_name: str):
        return load_handler_module(self.registry[skill_name]).execute

    async def test_write_action_creates_action(self) -> None:
        result = await self.handler("write-action")(
            self.db,
            self.owner_ctx,
            {"intent": "Call back", "due": "2026-04-01T14:00:00Z"},
        )
        self.assertIn("Created action", result)

        row = self.db.execute("SELECT * FROM actions WHERE intent = ?", ("Call back",)).fetchone()
        self.assertIsNotNone(row)

    async def test_read_actions_formats_pending_actions(self) -> None:
        insert_action(
            self.db,
            person_id=self.person.id,
            call_id=self.call.id,
            intent="Send proposal",
            source="mid-call",
        )
        result = await self.handler("read-actions")(self.db, self.owner_ctx, {})
        self.assertIn("Pending actions:", result)
        self.assertIn("Send proposal", result)

    async def test_edit_action_updates_status(self) -> None:
        action = insert_action(
            self.db,
            person_id=self.person.id,
            call_id=self.call.id,
            intent="Review contract",
            source="mid-call",
        )
        result = await self.handler("edit-action")(
            self.db,
            self.owner_ctx,
            {"id": action.id, "status": "completed", "result": "done"},
        )
        self.assertIn("status updated to: completed", result)
        stored = get_action_by_id(self.db, action.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.status, "completed")

    async def test_write_fact_persists_content_and_embedding(self) -> None:
        module = load_handler_module(self.registry["write-fact"])
        with patch.object(
            module,
            "embed_chunks",
            new=AsyncMock(return_value=[make_embedding([0.1, 0.2])]),
        ):
            result = await module.execute(
                self.db,
                self.owner_ctx,
                {"content": "Prefers email", "factType": "preference"},
            )

        self.assertIn("Recorded fact", result)
        facts = get_active_facts_by_person(self.db, self.person.id)
        self.assertEqual([fact.content for fact in facts], ["Prefers email"])
        self.assertIsNotNone(facts[0].embedding)

    async def test_read_transcripts_formats_search_results(self) -> None:
        module = load_handler_module(self.registry["read-transcripts"])
        results = [SearchResult(id="chunk-1", content="Alice asked about Tuesday.", score=0.9)]
        with patch.object(module, "hybrid_search", new=AsyncMock(return_value=results)):
            output = await module.execute(self.db, self.public_ctx, {"query": "Tuesday"})

        self.assertIn("Found 1 transcript excerpts", output)
        self.assertIn("Alice asked about Tuesday.", output)

    async def test_read_soul_handles_missing_file(self) -> None:
        (self.home / "SOUL.md").unlink()
        result = await self.handler("read-soul")(self.db, self.owner_ctx, {})
        self.assertEqual(result, "SOUL.md not found.")

    async def test_write_person_validates_and_creates_contact(self) -> None:
        invalid = await self.handler("write-person")(self.db, self.owner_ctx, {"phone": "555-1234"})
        self.assertIn("Invalid phone number format", invalid)

        result = await self.handler("write-person")(
            self.db,
            self.owner_ctx,
            {"phone": "+15550002222", "name": "Bob"},
        )
        self.assertIn("Created contact", result)
        person = get_person_by_phone(self.db, "+15550002222")
        self.assertIsNotNone(person)
        assert person is not None
        self.assertEqual(person.name, "Bob")

    async def test_take_message_creates_action_and_notifies(self) -> None:
        module = load_handler_module(self.registry["take-message"])
        with patch.object(module, "notify", new=AsyncMock(return_value=True)) as mock_notify:
            result = await module.execute(
                self.db,
                self.public_ctx,
                {"content": "Please let me know when they're free.", "urgency": "high"},
            )

        self.assertIn("I've recorded this message", result)
        self.assertIn("Please let me know when they're free.", result)
        row = self.db.execute(
            "SELECT intent, urgency, source FROM actions WHERE call_id = ? ORDER BY created_at DESC LIMIT 1",
            (self.call.id,),
        ).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["intent"], "Message: Please let me know when they're free.")
        self.assertEqual(row["urgency"], "high")
        self.assertEqual(row["source"], "mid-call")
        mock_notify.assert_awaited_once_with(
            "TestBot",
            "Message from Alice: Please let me know when they're free.",
        )

    async def test_take_message_requires_content(self) -> None:
        result = await self.handler("take-message")(self.db, self.public_ctx, {"content": "   "})
        self.assertEqual(result, "Please provide a message to record.")

    async def test_send_email_no_config(self) -> None:
        module = load_handler_module(self.registry["send-email"])
        with patch.object(module, "get_smtp_config", return_value=None):
            result = await module.execute(
                self.db,
                self.owner_ctx,
                {"to": "test@example.com", "subject": "Hi", "body": "Hello"},
            )
        self.assertIn("SMTP not configured", result)

    async def test_send_email_missing_to(self) -> None:
        module = load_handler_module(self.registry["send-email"])
        with patch.object(module, "get_smtp_config", return_value=_smtp_config()):
            result = await module.execute(
                self.db,
                self.owner_ctx,
                {"subject": "Hi", "body": "Hello"},
            )
        self.assertIn("recipient", result.lower())

    async def test_send_email_invalid_address(self) -> None:
        module = load_handler_module(self.registry["send-email"])
        with patch.object(module, "get_smtp_config", return_value=_smtp_config()):
            result = await module.execute(
                self.db,
                self.owner_ctx,
                {"to": "notanemail", "subject": "Hi", "body": "Hello"},
            )
        self.assertIn("Invalid email", result)

    async def test_send_email_success(self) -> None:
        module = load_handler_module(self.registry["send-email"])
        with (
            patch.object(module, "get_smtp_config", return_value=_smtp_config()),
            patch.object(module, "send_email", new=AsyncMock()) as mock_send,
        ):
            result = await module.execute(
                self.db,
                self.owner_ctx,
                {"to": "test@example.com", "subject": "Hi", "body": "Hello"},
            )
        self.assertIn("Email sent", result)
        mock_send.assert_awaited_once_with("test@example.com", "Hi", "Hello")

    async def test_chat_handler_returns_message_text(self) -> None:
        result = await self.handler("chat")(
            self.db,
            self.owner_ctx,
            {"message": "  **Details**\n\n- one  "},
        )
        self.assertEqual(result, "**Details**\n\n- one")

    async def test_chat_handler_requires_message(self) -> None:
        result = await self.handler("chat")(self.db, self.owner_ctx, {"message": "   "})
        self.assertEqual(result, "No message provided.")

    async def test_edit_person_updates_name_by_phone(self) -> None:
        result = await self.handler("edit-person")(
            self.db,
            self.owner_ctx,
            {"phone": self.person.phone, "name": "Alice Smith"},
        )
        self.assertIn("Updated name", result)
        person = get_person_by_phone(self.db, self.person.phone)
        self.assertIsNotNone(person)
        assert person is not None
        self.assertEqual(person.name, "Alice Smith")

    async def test_read_facts_can_lookup_named_person(self) -> None:
        bob = upsert_person(self.db, "+15550002222", "Bob")
        insert_fact(
            self.db,
            person_id=bob.id,
            type="context",
            content="Bob prefers afternoon calls.",
            confidence=0.8,
            source="owner",
        )
        result = await self.handler("read-facts")(self.db, self.owner_ctx, {"person": "Bob"})
        self.assertIn("Bob prefers afternoon calls.", result)

    async def test_read_calls_returns_recent_call_summaries(self) -> None:
        update_call_summary(self.db, self.call.id, "Discussed onboarding")
        update_call_end(self.db, self.call.id, duration=120)
        result = await self.handler("read-calls")(self.db, self.owner_ctx, {})
        self.assertIn("Recent calls:", result)
        self.assertIn("Discussed onboarding", result)

    async def test_read_faq_joins_top_matches(self) -> None:
        module = load_handler_module(self.registry["read-faq"])
        matches = [
            SearchResult(id="faq-1", content="Answer one", score=0.9),
            SearchResult(id="faq-2", content="Answer two", score=0.8),
        ]
        with patch.object(module, "hybrid_search", new=AsyncMock(return_value=matches)):
            result = await module.execute(self.db, self.owner_ctx, {"query": "help"})
        self.assertIn("Answer one", result)
        self.assertIn("Answer two", result)

    async def test_supersede_fact_archives_and_excludes_from_active(self) -> None:
        fact = insert_fact(
            self.db,
            person_id=self.person.id,
            type="context",
            content="Likes morning calls.",
            confidence=0.8,
            source="owner",
        )
        result = await self.handler("supersede-fact")(self.db, self.owner_ctx, {"id": fact.id})
        self.assertIn("Superseded fact", result)
        self.assertIn("Likes morning calls.", result)
        self.assertEqual(get_active_facts_by_person(self.db, self.person.id), [])
        stored = get_fact_by_id(self.db, fact.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertIsNotNone(stored.superseded_at)

    async def test_supersede_fact_rejects_already_superseded(self) -> None:
        fact = insert_fact(
            self.db,
            person_id=self.person.id,
            type="context",
            content="Stale fact.",
            confidence=0.5,
            source="owner",
        )
        await self.handler("supersede-fact")(self.db, self.owner_ctx, {"id": fact.id})
        result = await self.handler("supersede-fact")(self.db, self.owner_ctx, {"id": fact.id})
        self.assertIn("already superseded", result)

    async def test_supersede_fact_rejects_missing_id(self) -> None:
        result = await self.handler("supersede-fact")(self.db, self.owner_ctx, {"id": "nonexistent"})
        self.assertIn("Fact not found", result)

    async def test_read_people_returns_matches(self) -> None:
        result = await self.handler("read-people")(self.db, self.owner_ctx, {"query": "Alice"})
        self.assertIn("Found 1 people", result)
        self.assertIn("Alice", result)

    async def test_read_search_surfaces_errors(self) -> None:
        module = load_handler_module(self.registry["read-search"])
        with patch.object(module, "invoke_agent", new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = await module.execute(self.db, self.owner_ctx, {"query": "weather"})
        self.assertEqual(result, "Search unavailable right now: boom")

    async def test_write_identity_updates_identity_file_and_agent_name(self) -> None:
        result = await self.handler("write-identity")(
            self.db,
            self.owner_ctx,
            {
                "name": "Lyra",
                "creature": "owl",
                "vibe": "calm",
                "emoji": ":)",
            },
        )
        self.assertIn("Identity written!", result)
        self.assertIn("Lyra", (self.home / "IDENTITY.md").read_text(encoding="utf-8"))
        self.assertEqual(get_agent_config().agent.name, "Lyra")

    async def test_write_soul_persists_content(self) -> None:
        result = await self.handler("write-soul")(
            self.db,
            self.owner_ctx,
            {"content": "# New Soul\n\nStay precise."},
        )
        self.assertIn("Soul written!", result)
        self.assertEqual(read_soul(), "# New Soul\n\nStay precise.")

    async def test_recall_self_lists_journal(self) -> None:
        with patch("mystic.config.time.time", return_value=1.0):
            write_soul("# New Soul\n\nStay precise.", trigger="edit-soul", note="Made it warmer")

        result = await self.handler("recall-self")(self.db, self.owner_ctx, {})

        self.assertIn("Recent journal entries for SOUL.md:", result)
        self.assertIn("trigger=edit-soul", result)
        self.assertIn("timestamp=1000", result)
        self.assertIn("note=Made it warmer", result)

    async def test_recall_self_reads_entry(self) -> None:
        with patch("mystic.config.time.time", return_value=1.0):
            write_soul("# New Soul\n\nStay precise.", trigger="edit-soul", note="Made it warmer")

        result = await self.handler("recall-self")(
            self.db,
            self.owner_ctx,
            {"timestamp": "1000"},
        )

        self.assertIn("SOUL.md snapshot from", result)
        self.assertIn("Trigger: edit-soul", result)
        self.assertIn("Timestamp: 1000", result)
        self.assertIn("Made it warmer", result)
        self.assertIn("Test Soul", result)

    async def test_edit_config_updates_allowlisted_field_and_rejects_locked_field(self) -> None:
        allowed = await self.handler("edit-config")(
            self.db,
            self.owner_ctx,
            {"file": "agent", "path": "agent.name", "value": "Nova"},
        )
        rejected = await self.handler("edit-config")(
            self.db,
            self.owner_ctx,
            {"file": "providers", "path": "twilio.authToken", "value": "secret"},
        )

        self.assertIn("Updated agent.json", allowed)
        self.assertEqual(get_agent_config().agent.name, "Nova")
        self.assertIn("not an editable field", rejected)

    async def test_edit_prompt_updates_prompt_file(self) -> None:
        prompt_file = self.home / "prompts" / "public" / "workflow.md"
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text("Old content", encoding="utf-8")
        module = load_handler_module(self.registry["edit-prompt"])

        with patch.object(module, "invoke_agent", new=AsyncMock(return_value="New content")):
            result = await module.execute(
                "system prompt",
                "",
                {"file": "public/workflow.md", "instruction": "Rewrite it"},
                {},
            )

        self.assertEqual(result, "Updated prompt file: public/workflow.md")
        self.assertEqual(prompt_file.read_text(encoding="utf-8"), "New content")

    async def test_edit_soul_rewrites_soul_file(self) -> None:
        module = load_handler_module(self.registry["edit-soul"])
        with patch.object(module, "invoke_agent", new=AsyncMock(return_value="# Soul\n\nBe kind.")):
            result = await module.execute("system prompt", "Be kinder.", {}, {})

        self.assertEqual(result, "Updated SOUL.md. Previous version saved to journal.")
        self.assertEqual(read_soul(), "# Soul\n\nBe kind.")
        self.assertEqual(len(list_journal_entries("soul")), 1)

    async def test_read_transcripts_requires_query(self) -> None:
        result = await self.handler("read-transcripts")(self.db, self.owner_ctx, {})
        self.assertEqual(result, "Please provide a search query for transcripts.")

    async def test_write_action_rejects_invalid_due_date(self) -> None:
        result = await self.handler("write-action")(
            self.db,
            self.owner_ctx,
            {"intent": "Call back", "due": "not-a-date"},
        )
        self.assertIn("Could not parse due date", result)

    async def test_read_setup_reports_missing_tailscale(self) -> None:
        module = load_handler_module(self.registry["read-setup"])
        status = type(
            "Status",
            (),
            {
                "identity": False,
                "soul": False,
                "tailscale_installed": False,
                "tailscale_reason": "not installed",
                "twilio": False,
            },
        )()
        with patch.object(module, "get_setup_status", return_value=status):
            result = await module.execute(self.db, self.owner_ctx, {})

        self.assertIn("Identity: not set", result)
        self.assertIn("Install: curl -fsSL https://tailscale.com/install.sh | sh", result)
        self.assertIn("Twilio: not configured", result)

    async def test_check_tailscale_reports_hostname_and_funnel_status(self) -> None:
        module = load_handler_module(self.registry["check-tailscale"])
        with (
            patch.object(module, "check_tailscale_ready", return_value=(True, "")),
            patch.object(module, "get_tailscale_hostname", return_value="agent.tail1234.ts.net"),
            patch.object(
                module,
                "get_tailscale_funnel_status",
                return_value=(True, "https://agent.tail1234.ts.net/"),
            ),
        ):
            result = await module.execute(self.db, self.owner_ctx, {})

        self.assertIn("Tailscale is ready.", result)
        self.assertIn("Hostname: https://agent.tail1234.ts.net", result)
        self.assertIn("Funnel status:\nhttps://agent.tail1234.ts.net/", result)

    async def test_write_twilio_credentials_saves_draft_config(self) -> None:
        providers_path = self.home / "config" / "providers.json"
        providers_payload = json.loads(providers_path.read_text(encoding="utf-8"))
        providers_payload.pop("twilio", None)
        providers_path.write_text(json.dumps(providers_payload, indent=2) + "\n", encoding="utf-8")

        module = load_handler_module(self.registry["write-twilio-credentials"])
        response = type("Response", (), {"status_code": 200})()
        with patch.object(module, "fetch_with_timeout", new=AsyncMock(return_value=response)):
            result = await module.execute(
                self.db,
                self.owner_ctx,
                {"account_sid": "AC123", "auth_token": "secret"},
            )

        self.assertEqual(result, "Twilio credentials validated and saved.")
        saved = json.loads(providers_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["twilioDraft"], {"accountSid": "AC123", "authToken": "secret"})
        self.assertNotIn("twilio", saved)

    async def test_write_twilio_number_search_uses_saved_draft_credentials(self) -> None:
        providers_path = self.home / "config" / "providers.json"
        providers_payload = json.loads(providers_path.read_text(encoding="utf-8"))
        providers_payload.pop("twilio", None)
        providers_payload["twilioDraft"] = {"accountSid": "AC123", "authToken": "secret"}
        providers_path.write_text(json.dumps(providers_payload, indent=2) + "\n", encoding="utf-8")

        module = load_handler_module(self.registry["write-twilio-number"])
        with patch.object(
            module,
            "search_available_numbers",
            new=AsyncMock(return_value=[{"phoneNumber": "+15550003333", "friendlyName": "Los Angeles, CA"}]),
        ) as search_mock:
            result = await module.execute(self.db, self.owner_ctx, {"area_code": "310"})

        self.assertIn("+15550003333", result)
        assert search_mock.await_args is not None
        self.assertEqual(search_mock.await_args.args[0].accountSid, "AC123")
        self.assertEqual(search_mock.await_args.kwargs["area_code"], "310")

    async def test_write_twilio_number_purchase_promotes_draft_to_full_twilio(self) -> None:
        providers_path = self.home / "config" / "providers.json"
        providers_payload = json.loads(providers_path.read_text(encoding="utf-8"))
        providers_payload.pop("twilio", None)
        providers_payload["twilioDraft"] = {"accountSid": "AC123", "authToken": "secret"}
        providers_path.write_text(json.dumps(providers_payload, indent=2) + "\n", encoding="utf-8")

        module = load_handler_module(self.registry["write-twilio-number"])
        with (
            patch.object(module, "get_tunnel_url", return_value="https://agent.tail1234.ts.net"),
            patch.object(module, "list_incoming_phone_numbers", new=AsyncMock(return_value=[])),
            patch.object(module, "ensure_phone_line_ready", new=AsyncMock(return_value=_phone_ready())) as ready_mock,
            patch.object(
                module,
                "buy_phone_number",
                new=AsyncMock(return_value={"phoneNumber": "+15550004444", "sid": "PN123"}),
            ) as buy_mock,
        ):
            result = await module.execute(self.db, self.owner_ctx, {"phone_number": "+15550004444"})

        self.assertEqual(
            result,
            "Purchased and saved +15550004444. "
            "Tunnel active at https://agent.tail1234.ts.net; Twilio webhooks verified.",
        )
        buy_mock.assert_awaited_once()
        assert buy_mock.await_args is not None
        buy_args = buy_mock.await_args.args
        self.assertEqual(buy_args[2], "https://agent.tail1234.ts.net/webhook/twilio/voice")
        self.assertEqual(buy_args[3], "https://agent.tail1234.ts.net/webhook/twilio/status")
        ready_mock.assert_awaited_once()
        saved = json.loads(providers_path.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["twilio"],
            {
                "accountSid": "AC123",
                "authToken": "secret",
                "phoneNumber": "+15550004444",
                "phoneNumberSid": "PN123",
            },
        )
        self.assertNotIn("twilioDraft", saved)

    async def test_write_twilio_number_attaches_owned_number(self) -> None:
        providers_path = self.home / "config" / "providers.json"
        providers_payload = json.loads(providers_path.read_text(encoding="utf-8"))
        providers_payload.pop("twilio", None)
        providers_payload["twilioDraft"] = {"accountSid": "AC123", "authToken": "secret"}
        providers_path.write_text(json.dumps(providers_payload, indent=2) + "\n", encoding="utf-8")

        module = load_handler_module(self.registry["write-twilio-number"])
        with (
            patch.object(module, "get_tunnel_url", return_value="https://agent.tail1234.ts.net"),
            patch.object(
                module,
                "list_incoming_phone_numbers",
                new=AsyncMock(
                    return_value=[
                        {"sid": "PNOWNED", "phoneNumber": "+15550004444", "friendlyName": "Existing"},
                    ],
                ),
            ) as list_mock,
            patch.object(module, "buy_phone_number", new=AsyncMock()) as buy_mock,
            patch.object(module, "ensure_phone_line_ready", new=AsyncMock(return_value=_phone_ready())) as ready_mock,
        ):
            result = await module.execute(self.db, self.owner_ctx, {"phone_number": "+15550004444"})

        self.assertEqual(
            result,
            "Attached and saved +15550004444. "
            "Tunnel active at https://agent.tail1234.ts.net; Twilio webhooks verified.",
        )
        list_mock.assert_awaited_once()
        buy_mock.assert_not_awaited()
        ready_mock.assert_awaited_once()
        saved = json.loads(providers_path.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["twilio"],
            {
                "accountSid": "AC123",
                "authToken": "secret",
                "phoneNumber": "+15550004444",
                "phoneNumberSid": "PNOWNED",
            },
        )
        self.assertNotIn("twilioDraft", saved)

    async def test_write_twilio_number_attaches_owned_number_by_last_four(self) -> None:
        providers_path = self.home / "config" / "providers.json"
        providers_payload = json.loads(providers_path.read_text(encoding="utf-8"))
        providers_payload.pop("twilio", None)
        providers_payload["twilioDraft"] = {"accountSid": "AC123", "authToken": "secret"}
        providers_path.write_text(json.dumps(providers_payload, indent=2) + "\n", encoding="utf-8")

        module = load_handler_module(self.registry["write-twilio-number"])
        with (
            patch.object(module, "get_tunnel_url", return_value=None),
            patch.object(module, "check_tailscale_ready", return_value=(False, "not installed")),
            patch.object(
                module,
                "list_incoming_phone_numbers",
                new=AsyncMock(
                    return_value=[
                        {"sid": "PN7192", "phoneNumber": "+15555077192", "friendlyName": "Owner Line"},
                        {"sid": "PN3304", "phoneNumber": "+15554303304", "friendlyName": "Spare"},
                    ],
                ),
            ),
            patch.object(module, "start_tunnel", new=AsyncMock()) as start_mock,
            patch.object(module, "buy_phone_number", new=AsyncMock()) as buy_mock,
        ):
            result = await module.execute(self.db, self.owner_ctx, {"phone_number": "7192"})

        self.assertEqual(result, "Attached and saved +15555077192. Tailscale tunnel was not activated: not installed.")
        start_mock.assert_not_awaited()
        buy_mock.assert_not_awaited()
        saved = json.loads(providers_path.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["twilio"],
            {
                "accountSid": "AC123",
                "authToken": "secret",
                "phoneNumber": "+15555077192",
                "phoneNumberSid": "PN7192",
            },
        )
        self.assertNotIn("twilioDraft", saved)

    async def test_write_twilio_number_purchase_starts_tunnel_when_ready(self) -> None:
        providers_path = self.home / "config" / "providers.json"
        providers_payload = json.loads(providers_path.read_text(encoding="utf-8"))
        providers_payload.pop("twilio", None)
        providers_payload["twilioDraft"] = {"accountSid": "AC123", "authToken": "secret"}
        providers_path.write_text(json.dumps(providers_payload, indent=2) + "\n", encoding="utf-8")

        module = load_handler_module(self.registry["write-twilio-number"])
        with (
            patch.object(module, "get_tunnel_url", return_value=None),
            patch.object(module, "list_incoming_phone_numbers", new=AsyncMock(return_value=[])),
            patch.object(module, "check_tailscale_ready", return_value=(True, "")),
            patch.object(module, "start_tunnel", new=AsyncMock(return_value="https://agent.tail1234.ts.net")) as start_mock,
            patch.object(module, "set_tunnel_url") as set_tunnel_mock,
            patch.object(module, "ensure_phone_line_ready", new=AsyncMock(return_value=_phone_ready())),
            patch.object(
                module,
                "buy_phone_number",
                new=AsyncMock(return_value={"phoneNumber": "+15550005555", "sid": "PN456"}),
            ),
        ):
            result = await module.execute(self.db, self.owner_ctx, {"phone_number": "+15550005555"})

        self.assertIn("Tunnel active at https://agent.tail1234.ts.net", result)
        start_mock.assert_awaited_once_with(get_agent_config().server.port)
        set_tunnel_mock.assert_called_once_with("https://agent.tail1234.ts.net")

    async def test_read_twilio_numbers_lists_inventory(self) -> None:
        providers_path = self.home / "config" / "providers.json"
        providers_payload = json.loads(providers_path.read_text(encoding="utf-8"))
        providers_payload["twilio"] = {
            "accountSid": "AC123",
            "authToken": "secret",
            "phoneNumber": "+15550006666",
        }
        providers_path.write_text(json.dumps(providers_payload, indent=2) + "\n", encoding="utf-8")

        module = load_handler_module(self.registry["read-twilio-numbers"])
        with patch.object(
            module,
            "list_incoming_phone_numbers",
            new=AsyncMock(
                return_value=[
                    {"sid": "PN111", "phoneNumber": "+15550006666", "friendlyName": "Owner Line"},
                    {"sid": "PN222", "phoneNumber": "+15550007777", "friendlyName": "Spare"},
                ]
            ),
        ) as list_mock:
            result = await module.execute(self.db, self.owner_ctx, {})

        list_mock.assert_awaited_once()
        assert list_mock.await_args is not None
        self.assertEqual(list_mock.await_args.args[0].accountSid, "AC123")
        self.assertIn("+15550006666", result)
        self.assertIn("(attached to this agent)", result)
        self.assertIn("+15550007777", result)

    async def test_read_twilio_numbers_handles_empty_inventory(self) -> None:
        providers_path = self.home / "config" / "providers.json"
        providers_payload = json.loads(providers_path.read_text(encoding="utf-8"))
        providers_payload.pop("twilio", None)
        providers_payload["twilioDraft"] = {"accountSid": "AC123", "authToken": "secret"}
        providers_path.write_text(json.dumps(providers_payload, indent=2) + "\n", encoding="utf-8")

        module = load_handler_module(self.registry["read-twilio-numbers"])
        with patch.object(
            module,
            "list_incoming_phone_numbers",
            new=AsyncMock(return_value=[]),
        ):
            result = await module.execute(self.db, self.owner_ctx, {})

        self.assertIn("No phone numbers", result)

    async def test_read_twilio_numbers_requires_credentials(self) -> None:
        providers_path = self.home / "config" / "providers.json"
        providers_payload = json.loads(providers_path.read_text(encoding="utf-8"))
        providers_payload.pop("twilio", None)
        providers_payload.pop("twilioDraft", None)
        providers_path.write_text(json.dumps(providers_payload, indent=2) + "\n", encoding="utf-8")

        module = load_handler_module(self.registry["read-twilio-numbers"])
        result = await module.execute(self.db, self.owner_ctx, {})
        self.assertIn("not configured", result)


if __name__ == "__main__":
    unittest.main()
