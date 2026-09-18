"""Streamlit workflow checks with an isolated HTTP boundary.

Run: python -m unittest admin.test_ui
These fixtures are used only by tests; production pages never display demo data.
"""

from __future__ import annotations

import unittest
import os
import time
from base64 import b64decode
from contextlib import chdir
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from admin.agents import SECTIONS
from admin.api import ApiError, FrontendAdapter, api


AGENT = {
    "id": "agent-test", "name": "Soporte de prueba", "description": "Validación de interfaz",
    "provider": "openai", "model": "test-chat-model", "embedding_provider": "openai",
    "embedding_model": "test-embedding-model", "max_interactions": 10, "tools_enabled": [],
    "scope_description": "Atención de productos", "out_of_scope_policy": "WARN",
    "system_prompt": "Responde con las fuentes disponibles.", "active": True,
    "published": False, "public_url": None, "logo_url": None,
}
LOGO = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2l9sAAAAASUVORK5CYII=")
FAQ = {"id": "faq-test", "question": "¿Qué incluye?", "answer": "Incluye soporte.", "tags": ["soporte"], "priority": 1, "active": True, "status": "INDEXED"}
QUESTION = {"id": "question-test", "agent_id": "agent-test", "question": "¿Qué incluye?", "response": "Incluye soporte.", "answered": True, "scope_status": "IN_SCOPE", "channel": "playground"}
CONVERSATION = {"id": "conversation-test", "agent_id": "agent-test", "user_id": "playground", "channel": "playground", "status": "ACTIVE", "interaction_count": 1, "messages": [{"role": "user", "content": "¿Qué incluye?"}, {"role": "assistant", "content": "Incluye soporte."}]}


def fake_get(path: str, **kwargs):
    if path == "/auth/session":
        return {"role": "superuser", "expires_at": int(time.time()) + 3600}
    if path == "/health":
        return {"status": "ok"}
    if path == "/api/agents":
        return [AGENT]
    if path == "/api/agents/agent-test":
        return AGENT
    if path == "/api/agents/agent-test/logo":
        assert kwargs.get("raw") is True
        return LOGO
    if path == "/api/analytics":
        return {"total_questions": 1, "unanswered": 0, "out_of_scope": 0, "by_day": [{"day": "2026-09-05", "count": 1}], "top_faqs": [{"id": "faq-test", "name": FAQ["question"], "count": 1}], "top_documents": [{"id": "doc-test", "name": "Manual.pdf", "count": 1}], "similar_questions": [{"question": QUESTION["question"], "count": 2, "question_ids": [QUESTION["id"]]}]}
    if path == "/api/credentials":
        return [{"provider": "openai", "configured": True}, {"provider": "gemini", "configured": False}]
    if path == "/api/jobs":
        return []
    if path == "/api/runtime/graph":
        return {"mermaid": "graph TD; A-->B"}
    if path == "/api/questions":
        return [QUESTION]
    if path == "/api/conversations":
        return [CONVERSATION]
    if path.startswith("/api/conversations/"):
        return CONVERSATION
    if path.endswith("/models"):
        if kwargs.get("params", {}).get("purpose") == "embeddings":
            return {"models": ["test-gemini-embedding-model" if "/gemini/" in path else AGENT["embedding_model"]]}
        return {"models": ["test-chat-model"]}
    if path.endswith("/prompts"):
        return [{"id": "prompt-test", "body": "Prompt de prueba", "created_at": "2026-09-05"}]
    if path.endswith("/faqs/export"):
        return b"question,answer\n"
    if path.endswith("/faqs"):
        return [FAQ]
    if path.endswith("/documents"):
        return [{"id": "document-test", "name": "Manual.pdf", "status": "INDEXED", "chunk_count": 3}]
    if path.endswith("/files"):
        return [{"id": "file-test", "name": "Manual.pdf", "kind": "DELIVERABLE"}]
    if path.endswith("/databases"):
        return [{"id": "database-test", "name": "Catálogo", "dialect": "postgresql", "allowed_tables": ["productos"], "max_rows": 100, "configured": True}]
    if path.endswith("/integrations"):
        return [{"id": "integration-test", "channel": "telegram", "enabled": False, "config": {}}]
    if path.endswith("/memory"):
        return [CONVERSATION]
    raise AssertionError(f"Ruta HTTP inesperada: {path}")


class AdminWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.read_patch = patch.object(api, "get", side_effect=fake_get)
        self.read_patch.start()
        self.addCleanup(self.read_patch.stop)

    def app(self, module: str, function: str) -> AppTest:
        return AppTest.from_string(f"from {module} import {function}\n{function}()", default_timeout=15)

    def assert_renders(self, app: AppTest) -> None:
        app.run()
        self.assertFalse(app.exception, [exception.message for exception in app.exception])

    def test_all_agent_sections_render_populated_records(self):
        for section in SECTIONS:
            with self.subTest(section=section):
                app = self.app("admin.agents", "agent_page")
                app.session_state["ui_agent_section"] = section
                self.assert_renders(app)

    def test_activity_pages_render_populated_records(self):
        for function in ("dashboard_page", "playground_page", "conversations_page", "questions_page", "analytics_page"):
            with self.subTest(function=function):
                self.assert_renders(self.app("admin.overview", function))

    def test_application_navigation_renders(self):
        app = AppTest.from_file(str(Path(__file__).with_name("app.py")), default_timeout=20)
        app.session_state["superuser_token"] = "test-superuser-session"
        app.session_state["superuser_expires_at"] = int(time.time()) + 3600
        self.assert_renders(app)

    def test_all_configuration_services_render(self):
        for service in ("OpenAI", "Google Gemini", "WhatsApp", "Telegram"):
            with self.subTest(service=service):
                app = self.app("admin.settings", "settings_page")
                app.session_state["ui_settings_section"] = service
                self.assert_renders(app)

    def test_playground_reloads_persisted_history_after_http_send(self):
        app = self.app("admin.overview", "playground_page")
        self.assert_renders(app)
        with patch.object(api, "post", return_value={"conversation_id": CONVERSATION["id"], "response": "Incluye soporte.", "interaction_count": 1, "conversation_status": "ACTIVE", "scope_status": "IN_SCOPE", "trace": ["scope_guard"]}) as send:
            app.chat_input[0].set_value("¿Qué incluye?").run()
        self.assertFalse(app.exception)
        send.assert_called_once()
        self.assertEqual(app.session_state["ui_playground_cid_agent-test"], CONVERSATION["id"])
        self.assertEqual(len(app.chat_message), 2)
        self.assertNotIn("messages", app.session_state.filtered_state)

    def test_provider_key_is_sent_to_http_adapter(self):
        app = self.app("admin.settings", "settings_page")
        self.assert_renders(app)
        next(widget for widget in app.text_input if widget.label == "API Key").input("test-key-not-a-real-credential")
        with patch.object(api, "put", return_value={"configured": True}) as save:
            next(button for button in app.button if button.label == "Guardar API key").click().run()
        self.assertFalse(app.exception)
        save.assert_called_once_with("/api/credentials/openai", {"api_key": "test-key-not-a-real-credential"})
        self.assertNotIn("api_key", app.session_state.filtered_state)

    def test_faq_create_uses_http_and_preserves_agent_scope(self):
        app = self.app("admin.agents", "agent_page")
        app.session_state["ui_agent_section"] = "FAQ"
        self.assert_renders(app)
        next(widget for widget in app.text_input if widget.label == "Pregunta").input("¿Hay garantía?")
        next(widget for widget in app.text_area if widget.label == "Respuesta").input("La garantía es de doce meses.")
        with patch.object(api, "post", return_value={"id": "created", "status": "PENDING"}) as save:
            next(button for button in app.button if button.label == "Crear FAQ").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(save.call_args.args[0], "/api/agents/agent-test/faqs")
        self.assertEqual(save.call_args.kwargs["json"]["question"], "¿Hay garantía?")

    def test_changing_embedding_provider_selects_its_own_model_catalog(self):
        app = self.app("admin.agents", "agent_page")
        app.session_state["ui_agent_section"] = "Modelo"
        self.assert_renders(app)
        self.assertEqual(next(widget for widget in app.selectbox if widget.label == "Modelo de embeddings").value, AGENT["embedding_model"])
        next(widget for widget in app.selectbox if widget.label == "Proveedor de embeddings").select("gemini").run()
        self.assertFalse(app.exception)
        embedding = next(widget for widget in app.selectbox if widget.label == "Modelo de embeddings")
        self.assertEqual(embedding.value, "test-gemini-embedding-model")
        self.assertNotIn(AGENT["embedding_model"], embedding.options)
        with patch.object(api, "patch", return_value={}) as save:
            next(button for button in app.button if button.label == "Guardar modelo").click().run()
        self.assertFalse(app.exception)
        save.assert_called_once_with("/api/agents/agent-test", {
            "provider": "openai", "model": AGENT["model"],
            "embedding_provider": "gemini", "embedding_model": "test-gemini-embedding-model",
        })

    def test_changing_embedding_provider_without_catalog_does_not_reuse_previous_model(self):
        def read(path, **kwargs):
            if path == "/api/providers/gemini/models" and kwargs.get("params", {}).get("purpose") == "embeddings":
                raise ApiError("Falta configurar la credencial requerida")
            return fake_get(path, **kwargs)

        with patch.object(api, "get", side_effect=read):
            app = self.app("admin.agents", "agent_page")
            app.session_state["ui_agent_section"] = "Modelo"
            self.assert_renders(app)
            next(widget for widget in app.selectbox if widget.label == "Proveedor de embeddings").select("gemini").run()
            manual = next(widget for widget in app.text_input if widget.label == "ID de modelo de embeddings")
            self.assertEqual(manual.value, "")
            with patch.object(api, "patch") as save:
                next(button for button in app.button if button.label == "Guardar modelo").click().run()
        self.assertFalse(app.exception)
        save.assert_not_called()
        self.assertTrue(any("Indica el modelo de conversación y el modelo de embeddings" in error.value for error in app.error))

    def test_jobs_require_review_before_allowing_resend(self):
        job = {"id": "job-review-test", "kind": "channel_delivery", "status": "NEEDS_REVIEW", "attempts": 1, "error": "Resultado del envío desconocido"}
        def read(path, **kwargs):
            return [job] if path == "/api/jobs" else fake_get(path, **kwargs)
        with patch.object(api, "get", side_effect=read):
            app = self.app("admin.agents", "agent_page")
            app.session_state["ui_agent_section"] = "Canales"
            self.assert_renders(app)
            retry = next(button for button in app.button if button.label == "Reintentar trabajo")
            self.assertTrue(retry.disabled)
            next(widget for widget in app.checkbox if widget.label == "Revisé el envío y autorizo reenviarlo").check().run()
            with patch.object(api, "post", return_value={"status": "PENDING"}) as resend:
                next(button for button in app.button if button.label == "Reintentar trabajo").click().run()
        self.assertFalse(app.exception)
        resend.assert_called_once_with("/api/jobs/job-review-test/retry", json={"allow_resend": True})

    def test_logo_upload_uses_dedicated_multipart_endpoint(self):
        uploaded = SimpleNamespace(name="mi-logo.png", size=len(LOGO), type="image/png", getvalue=lambda: LOGO)
        app = self.app("admin.agents", "agent_page")
        app.session_state["ui_agent_section"] = "Apariencia"
        with patch("admin.agents.st.file_uploader", return_value=uploaded), patch.object(api, "upload", return_value={"logo_url": "/api/agents/agent-test/logo"}) as upload:
            self.assert_renders(app)
            next(button for button in app.button if button.label == "Guardar logo").click().run()
        self.assertFalse(app.exception)
        upload.assert_called_once_with("/api/agents/agent-test/logo", uploaded)

    def test_oversized_logo_cannot_be_submitted(self):
        uploaded = SimpleNamespace(name="grande.png", size=2 * 1024 * 1024 + 1, type="image/png")
        app = self.app("admin.agents", "agent_page")
        app.session_state["ui_agent_section"] = "Apariencia"
        with patch("admin.agents.st.file_uploader", return_value=uploaded), patch.object(api, "upload") as upload:
            self.assert_renders(app)
        self.assertTrue(next(button for button in app.button if button.label == "Guardar logo").disabled)
        self.assertTrue(any("2 MB" in error.value for error in app.error))
        upload.assert_not_called()

    def test_current_logo_is_fetched_through_authenticated_adapter_and_can_be_deleted(self):
        with patch.dict(AGENT, {"logo_url": "/api/agents/agent-test/logo"}), patch.object(api, "get", side_effect=fake_get) as read:
            app = self.app("admin.agents", "agent_page")
            app.session_state["ui_agent_section"] = "Apariencia"
            self.assert_renders(app)
            read.assert_any_call("/api/agents/agent-test/logo", raw=True)
            with patch.object(api, "delete", return_value=None) as remove:
                next(button for button in app.button if button.label == "Eliminar logo").click().run()
        self.assertFalse(app.exception)
        remove.assert_called_once_with("/api/agents/agent-test/logo")

    def test_publishing_displays_the_public_chat_url_returned_by_backend(self):
        public_url = "https://agentes.example/chat/agent-test"
        app = self.app("admin.agents", "agent_page")
        app.session_state["ui_agent_section"] = "Publicación"
        self.assert_renders(app)
        with patch.object(api, "post", return_value={"published": True, "public_url": public_url}) as publish:
            next(button for button in app.button if button.label == "Publicar página").click().run()
        self.assertFalse(app.exception)
        publish.assert_called_once_with("/api/agents/agent-test/publish")
        self.assertTrue(any(code.value == public_url for code in app.code))
        self.assertTrue(any(element.proto.url == public_url for element in app.get("link_button")))

    def test_unconfigured_agent_cannot_publish(self):
        def read(path, **kwargs):
            if path == "/api/credentials":
                return []
            return fake_get(path, **kwargs)

        with patch.dict(AGENT, {"model": "", "active": False}), patch.object(api, "get", side_effect=read), patch.object(api, "post") as publish:
            app = self.app("admin.agents", "agent_page")
            app.session_state["ui_agent_section"] = "Publicación"
            self.assert_renders(app)
        self.assertTrue(next(button for button in app.button if button.label == "Publicar página").disabled)
        publish.assert_not_called()

    def test_backend_publish_failure_does_not_show_a_public_link(self):
        app = self.app("admin.agents", "agent_page")
        app.session_state["ui_agent_section"] = "Publicación"
        self.assert_renders(app)
        with patch.object(api, "post", side_effect=ApiError("Configura la credencial del proveedor antes de publicar.")):
            next(button for button in app.button if button.label == "Publicar página").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("credencial" in error.value for error in app.error))
        self.assertFalse(app.get("link_button"))

    def test_publication_can_be_withdrawn_via_dedicated_endpoint(self):
        with patch.dict(AGENT, {"published": True, "public_url": "https://agentes.example/chat/agent-test"}):
            app = self.app("admin.agents", "agent_page")
            app.session_state["ui_agent_section"] = "Publicación"
            self.assert_renders(app)
            with patch.object(api, "delete", return_value={"published": False}) as withdraw:
                next(button for button in app.button if button.label == "Retirar publicación").click().run()
        self.assertFalse(app.exception)
        withdraw.assert_called_once_with("/api/agents/agent-test/publish")


class AdapterCredentialTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(chdir(self.directory))
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def test_local_dotenv_supplies_api_url_but_does_not_grant_admin_access(self):
        (self.directory / ".env").write_text(
            "API_BASE_URL=http://local-backend:8000/\n"
            "ADMIN_TOKEN=test-dotenv-token\n"
            "DATABASE_URL=unrelated-backend-setting\n",
            encoding="utf-8",
        )
        adapter = FrontendAdapter()
        self.assertEqual(adapter.base_url, "http://local-backend:8000")
        self.assertEqual(adapter.token, "")
        self.assertNotIn("ADMIN_TOKEN", os.environ)

    def test_process_environment_overrides_dotenv_values(self):
        (self.directory / ".env").write_text(
            "API_BASE_URL=http://dotenv-backend:8000\nADMIN_TOKEN=test-dotenv-token\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"API_BASE_URL": "http://environment-backend:8000", "ADMIN_TOKEN": "test-environment-token"}):
            adapter = FrontendAdapter()
        self.assertEqual(adapter.base_url, "http://environment-backend:8000")
        self.assertEqual(adapter.token, "")

    def test_dotenv_token_file_does_not_grant_access_to_anonymous_visitors(self):
        (self.directory / "admin_token").write_text("test-local-file-token\n", encoding="utf-8")
        (self.directory / ".env").write_text(
            "ADMIN_TOKEN_FILE=admin_token\nADMIN_TOKEN=ignored-inline-token\n",
            encoding="utf-8",
        )
        self.assertEqual(FrontendAdapter().token, "")

    def test_server_token_is_not_sent_as_http_header_without_a_user_session(self):
        with patch.dict(os.environ, {"ADMIN_TOKEN_FILE": "/run/agente/admin_token", "ADMIN_TOKEN": "ignored-inline-token"}), patch("admin.api.httpx.Client") as client_type:
            response = client_type.return_value.__enter__.return_value.request.return_value
            response.is_error = False
            response.status_code = 200
            response.content = b'{"status":"ok"}'
            response.json.return_value = {"status": "ok"}
            adapter = FrontendAdapter(base_url="http://backend:8000")
            self.assertEqual(adapter.get("/health"), {"status": "ok"})
            client_type.return_value.__enter__.return_value.request.assert_called_once_with("GET", "http://backend:8000/health", headers={})

    def test_explicit_constructor_token_takes_precedence_over_callback(self):
        with patch.dict(os.environ, {"API_BASE_URL": "http://environment-backend:8000", "ADMIN_TOKEN_FILE": "/run/agente/admin_token"}):
            adapter = FrontendAdapter(base_url="http://explicit-backend:8000", token="explicit-test-token", token_provider=lambda: "ignored-callback-token")
            self.assertEqual(adapter.base_url, "http://explicit-backend:8000")
            self.assertEqual(adapter.token, "explicit-test-token")

    def test_callback_token_is_evaluated_for_each_request_and_sent_only_in_header(self):
        session = {"token": "first-test-session"}
        adapter = FrontendAdapter(base_url="http://backend:8000", token_provider=lambda: session["token"])
        with patch("admin.api.httpx.Client") as client_type:
            response = client_type.return_value.__enter__.return_value.request.return_value
            response.is_error = False
            response.status_code = 200
            response.content = b'{"status":"ok"}'
            response.json.return_value = {"status": "ok"}
            adapter.get("/health")
            session["token"] = "second-test-session"
            adapter.get("/health")
            session["token"] = ""
            adapter.get("/health")
            calls = client_type.return_value.__enter__.return_value.request.call_args_list
        self.assertEqual([call.kwargs["headers"] for call in calls], [
            {"Authorization": "Bearer first-test-session"},
            {"Authorization": "Bearer second-test-session"},
            {},
        ])


if __name__ == "__main__":
    unittest.main()
